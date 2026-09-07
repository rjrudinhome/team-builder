"""CSV-first deterministic team-building pipeline (Python standard library only)."""
from __future__ import annotations

import csv
import json
import re
import subprocess
from collections import Counter as collections_counter, defaultdict
from itertools import combinations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")


LOW_AVAILABILITY_SLOTS = 5  # coach requesters below this are protected when choosing a practice slot
LOW_AVAILABILITY_WARNING = 3  # requesters below this get an outreach warning
SEED_TARGET = 8  # teams with fewer anchor members are seeded by a large friend web


def git_root_for(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def ensure_private_output_file(path: Path) -> Path:
    output_file = path.expanduser().resolve()
    repository_root = git_root_for(output_file)
    if repository_root is None:
        return output_file
    relative_output_file = output_file.relative_to(repository_root)
    ignored = subprocess.run(
        ["git", "-C", str(repository_root), "check-ignore", "--quiet", str(relative_output_file)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0
    if not ignored:
        raise ValueError(
            f"output artifact {output_file} is inside a Git repository but is not Git-ignored; "
            "roster artifacts contain private registration data"
        )
    return output_file


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean(value).lower()).strip()


def tokens(value: str) -> set[str]:
    return set(key(value).split())


def display_name(row: dict[str, str], first: str, last: str) -> str:
    return " ".join(x for x in (clean(row.get(first)), clean(row.get(last))) if x)


def availability(value: str) -> tuple[bool, bool, str]:
    """Return early, late and a stable availability code."""
    text = clean(value).lower()
    if not text or "unavailable" in text:
        return False, False, "unavailable"
    if "either" in text or "any time" in text:
        return True, True, "either"
    early = "early" in text
    late = "late" in text
    return early, late, "early" if early and not late else "late" if late and not early else "either" if early else "unavailable"


def split_requests(value: str) -> list[str]:
    text = clean(value)
    if not text:
        return []
    return [x.strip() for x in re.split(r"\s*(?:,|;|\band\b|&)\s*", text, flags=re.I) if x.strip()]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return [{clean(k): clean(v) for k, v in row.items()} for row in csv.DictReader(fh)]


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path = ensure_private_output_file(path)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({field: clean(row.get(field)) for field in fields} for row in rows)


ASSIGNED_PRACTICE_SLOT_FIELDS = ["Location", "Day", "Time", "Team", "Head Coach"]


def assigned_practice_slot_rows(
    teams: list[dict[str, str]],
    coach_by_id: dict[str, dict[str, str]],
    calendar_first: bool = False,
) -> list[dict[str, str]]:
    rows = [
        {
            "Location": team["location"],
            "Day": team["day"],
            "Time": team["time"],
            "Team": team["team_id"],
            "Head Coach": coach_by_id.get(team["head_coach_id"], {}).get("coach_name", ""),
        }
        for team in teams
        if team["slot_id"]
    ]
    day_order = {day: index for index, day in enumerate(DAYS)}
    time_order = {"Early": 0, "Late": 1}
    sort_key = (
        lambda row: (
            day_order.get(row["Day"], len(day_order)),
            time_order.get(row["Time"], len(time_order)),
            row["Location"].casefold(),
            row["Team"].casefold(),
        )
        if calendar_first
        else (
            row["Location"].casefold(),
            day_order.get(row["Day"], len(day_order)),
            time_order.get(row["Time"], len(time_order)),
            row["Team"].casefold(),
        )
    )
    return sorted(
        rows,
        key=sort_key,
    )


def players_with_no_friend_requests_met_count(request_outcomes: list[dict[str, str]]) -> int:
    friend_outcomes: dict[str, list[str]] = defaultdict(list)
    for outcome in request_outcomes:
        if outcome["request_type"] == "friend":
            friend_outcomes[outcome["source_reg_number"]].append(outcome["outcome"])
    return sum(
        all(outcome != "satisfied" for outcome in outcomes)
        for outcomes in friend_outcomes.values()
    )


def unmet_coach_request_reason(
    player: dict[str, str],
    requested_coach_teams: list[dict[str, str]],
    rosters: dict[str, list[str]],
    maximum_roster_size: int,
) -> str:
    compatible_teams = [
        team for team in requested_coach_teams if slot_ok(player, team)
    ]
    if requested_coach_teams and not compatible_teams:
        return "COACH_TEAM_SLOT_UNAVAILABLE"
    if compatible_teams and all(
        len(rosters[team["team_id"]]) >= maximum_roster_size
        for team in compatible_teams
    ):
        return "COACH_TEAM_FULL"
    return "COACH_ASSIGNED_TO_DIFFERENT_TEAM"


def slot_ok(person: dict[str, str], slot: dict[str, str]) -> bool:
    return person.get(f"{slot['day']}_{slot['time'].lower()}") == "yes"


def available_slots(person: dict[str, str], slots: list[dict[str, str]]) -> list[dict[str, str]]:
    return [slot for slot in slots if slot_ok(person, slot)]


def availability_pattern(person: dict[str, str]) -> str:
    codes = []
    for day in DAYS:
        early = person.get(f"{day}_early") == "yes"
        late = person.get(f"{day}_late") == "yes"
        codes.append("O" if early and late else "E" if early else "L" if late else "X")
    return "".join(codes)


def candidate_matches(query: str, records: list[dict[str, str]], name_field: str) -> list[tuple[dict[str, str], str]]:
    """Return conservative possible names; callers only auto-resolve singleton lists."""
    q = key(query)
    if not q or q in {"any coach", "any"}:
        return []
    exact = [(r, "exact_name") for r in records if key(r[name_field]) == q]
    return exact


@dataclass
class Pipeline:
    division: str
    input_dir: Path
    output_dir: Path
    config: dict[str, Any]
    overrides_path: Path | None = None
    players_path: Path | None = None
    projected_select_ids: frozenset[str] = field(default_factory=frozenset)

    def run(self) -> dict[str, int]:
        return run_divisions([self])[self.division]

    def prepare(self) -> None:
        if self.division not in self.config["team_counts"]:
            raise ValueError(f"Unknown division {self.division!r}")
        self.output_dir = self.output_dir.expanduser().resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for artifact in self.output_dir.glob("[0-9][0-9]_*.csv"):
            if artifact.is_file():
                artifact.unlink()
        self._normalise()
        self._validate()
        self._resolve_requests()
        self._load_overrides_and_relationships()
        self._diagnose()
        self._prepare_teams()

    def finish(self, final_vectors: dict[str, dict[str, tuple[Any, ...]]]) -> dict[str, int]:
        self._finish_slots(final_vectors)
        self._assign_players()
        self._report()
        return {"players": len(self.players), "teams": len(self.teams), "unassigned": len(self.unassigned)}

    def _normalise(self) -> None:
        labels = self.config["age_group_labels"]
        raw_players = read_csv(self.players_path or self.input_dir / "players.csv")
        raw_coaches = read_csv(self.input_dir / "coaches.csv")
        raw_slots = read_csv(self.input_dir / "practice-slots.csv")
        self.players: list[dict[str, str]] = []
        for raw in raw_players:
            name = display_name(raw, "Player First Name", "Player Last Name")
            group = clean(raw.get("Group Name"))
            row = {"reg_number": clean(raw.get("Reg Number")), "player_id": clean(raw.get("Reg Number")), "player_name": name,
                   "first_name": clean(raw.get("Player First Name")), "last_name": clean(raw.get("Player Last Name")),
                   "division": labels.get(group, ""), "source_group": group, "uuid": clean(raw.get("UUID")),
                   "school": clean(raw.get("School")), "friend_requests": clean(raw.get("FriendRequests")),
                   "coach_request": clean(raw.get("CoachRequest")), "volunteer_role": clean(raw.get("Volunteer Role")),
                   "submitter_name": clean(raw.get("Submitter Name")), "submitter_email": clean(raw.get("Submitter Email"))}
            for day in DAYS:
                early, late, code = availability(raw.get(day, ""))
                row.update({f"{day}_availability": code, f"{day}_early": "yes" if early else "no", f"{day}_late": "yes" if late else "no"})
            self.players.append(row)
        self.coaches: list[dict[str, str]] = []
        for raw in raw_coaches:
            name = display_name(raw, "Volunteer First Name", "Volunteer Last Name")
            group = clean(raw.get("Group Name"))
            row = {"coach_id": clean(raw.get("UUID")), "coach_name": name, "first_name": clean(raw.get("Volunteer First Name")),
                   "last_name": clean(raw.get("Volunteer Last Name")), "division": labels.get(group, ""), "source_group": group,
                   "role": clean(raw.get("Volunteer Role")), "coach_email": clean(raw.get("Volunteer Email")).lower(), "registered_child": clean(raw.get("RegChild")), "child_name": clean(raw.get("ChildName")), "requests": clean(raw.get("Requests"))}
            for day in DAYS:
                early, late, code = availability(raw.get(f"{day}_1", ""))
                row.update({f"{day}_availability": code, f"{day}_early": "yes" if early else "no", f"{day}_late": "yes" if late else "no"})
            self.coaches.append(row)
        # Otto can export the same volunteer once for each registered child. Merge
        # same-division UUID rows so a coach is not double-counted or discarded.
        merged_coaches: dict[tuple[str, str], dict[str, str]] = {}
        for coach in self.coaches:
            identity = (coach["coach_id"], coach["division"])
            existing = merged_coaches.get(identity)
            if existing is None:
                coach["source_row_count"] = "1"
                merged_coaches[identity] = coach
                continue
            existing["source_row_count"] = str(int(existing["source_row_count"]) + 1)
            if key(coach["role"]) == "head coach":
                existing["role"] = coach["role"]
            for day in DAYS:
                early = existing[f"{day}_early"] == "yes" or coach[f"{day}_early"] == "yes"
                late = existing[f"{day}_late"] == "yes" or coach[f"{day}_late"] == "yes"
                existing[f"{day}_early"] = "yes" if early else "no"
                existing[f"{day}_late"] = "yes" if late else "no"
                existing[f"{day}_availability"] = "either" if early and late else "early" if early else "late" if late else "unavailable"
            if coach["requests"] and coach["requests"] not in existing["requests"]:
                existing["requests"] = "; ".join(part for part in (existing["requests"], coach["requests"]) if part)
        self.coaches = list(merged_coaches.values())
        self.slots = [{"slot_id": f"slot-{i:03d}", "day": clean(r.get("Day")), "time": clean(r.get("Time")), "location": clean(r.get("Location"))}
                      for i, r in enumerate(raw_slots, 1)]
        person_fields = ["reg_number", "player_id", "player_name", "first_name", "last_name", "division", "source_group", "uuid", "school", "friend_requests", "coach_request", "volunteer_role", "submitter_name", "submitter_email"] + [f"{d}_{x}" for d in DAYS for x in ("availability", "early", "late")]
        coach_fields = ["coach_id", "coach_name", "first_name", "last_name", "division", "source_group", "role", "registered_child", "requests", "source_row_count"] + [f"{d}_{x}" for d in DAYS for x in ("availability", "early", "late")]
        write_csv(self.output_dir / "01_players_normalized.csv", person_fields, self.players)
        write_csv(self.output_dir / "01_coaches_normalized.csv", coach_fields, self.coaches)
        write_csv(self.output_dir / "01_practice_slots_normalized.csv", ["slot_id", "day", "time", "location"], self.slots)
        self.players = sorted((p for p in self.players if p["division"] == self.division), key=lambda p: p["reg_number"])
        projected_select = [p for p in self.players if p["reg_number"] in self.projected_select_ids]
        self.players = [p for p in self.players if p["reg_number"] not in self.projected_select_ids]
        write_csv(
            self.output_dir / "00_projected_select_exclusions.csv",
            ["reg_number", "player_name", "division", "submitter_name", "submitter_email", "exclusion_type"],
            [
                {
                    "reg_number": p["reg_number"],
                    "player_name": p["player_name"],
                    "division": p["division"],
                    "submitter_name": p["submitter_name"],
                    "submitter_email": p["submitter_email"],
                    "exclusion_type": "projected_select",
                }
                for p in projected_select
            ],
        )
        self.coaches = sorted((c for c in self.coaches if c["division"] == self.division), key=lambda c: c["coach_id"])
        self.player_by_id = {p["reg_number"]: p for p in self.players if p["reg_number"]}
        self.coach_by_id = {c["coach_id"]: c for c in self.coaches if c["coach_id"]}
        self.slot_by_id = {s["slot_id"]: s for s in self.slots}

    def _validate(self) -> None:
        issues: list[dict[str, str]] = []
        for kind, records, ident in (("player", self.players, "reg_number"), ("coach", self.coaches, "coach_id")):
            seen: set[str] = set()
            for record in records:
                value = record[ident]
                if not value:
                    issues.append({"stage": "normalization", "severity": "error", "entity_type": kind, "entity_id": "", "issue": f"missing {ident}", "detail": record.get("player_name", record.get("coach_name", ""))})
                elif value in seen:
                    issues.append({"stage": "normalization", "severity": "error", "entity_type": kind, "entity_id": value, "issue": f"duplicate {ident}", "detail": ""})
                seen.add(value)
        for player in self.players:
            if not player["player_name"]:
                issues.append({"stage": "normalization", "severity": "error", "entity_type": "player", "entity_id": player["reg_number"], "issue": "missing player name", "detail": ""})
            if not any(player[f"{d}_{t}"] == "yes" for d in DAYS for t in ("early", "late")):
                issues.append({"stage": "availability", "severity": "error", "entity_type": "player", "entity_id": player["reg_number"], "issue": "no available practice time", "detail": ""})
        self.validation_issues = issues
        write_csv(self.output_dir / "01_validation_issues.csv", ["stage", "severity", "entity_type", "entity_id", "issue", "detail"], issues)

    def _resolve_requests(self) -> None:
        friend_candidates: list[dict[str, str]] = []
        coach_candidates: list[dict[str, str]] = []
        resolutions: list[dict[str, str]] = []
        self.friend_resolutions: list[dict[str, str]] = []
        self.coach_resolutions: list[dict[str, str]] = []
        for player in self.players:
            for index, request in enumerate(split_requests(player["friend_requests"]), 1):
                request_id = f"friend:{player['reg_number']}:{index}"
                matches = [(candidate, method) for candidate, method in candidate_matches(request, self.players, "player_name") if candidate["reg_number"] != player["reg_number"]]
                for candidate, method in matches:
                    friend_candidates.append({"request_id": request_id, "source_reg_number": player["reg_number"], "source_name": player["player_name"], "requested_name": request, "candidate_reg_number": candidate["reg_number"], "candidate_name": candidate["player_name"], "match_method": method})
                status = "resolved" if len(matches) == 1 else "ambiguous" if len(matches) > 1 else "unresolved"
                target = matches[0][0]["reg_number"] if status == "resolved" else ""
                row = {"request_id": request_id, "request_type": "friend", "source_reg_number": player["reg_number"], "source_name": player["player_name"], "requested_name": request, "status": status, "resolved_id": target, "resolved_name": matches[0][0]["player_name"] if target else "", "candidate_count": str(len(matches)), "reason": "auto-resolved only when exactly one candidate"}
                resolutions.append(row); self.friend_resolutions.append(row)
            request = clean(player["coach_request"])
            if request:
                request_id = f"coach:{player['reg_number']}:1"
                if key(request) in {"any coach", "any"}:
                    matches: list[tuple[dict[str, str], str]] = []
                    status, target, reason = "any_coach", "", "any coach request"
                elif len(split_requests(request)) != 1:
                    matches = []
                    status, target, reason = "invalid", "", "coach requests must name exactly one coach"
                else:
                    matches = candidate_matches(request, self.coaches, "coach_name")
                    status = "resolved" if len(matches) == 1 else "ambiguous" if len(matches) > 1 else "unresolved"
                    target = matches[0][0]["coach_id"] if status == "resolved" else ""
                    reason = "auto-resolved only when exactly one exact-name candidate"
                for candidate, method in matches:
                    coach_candidates.append({"request_id": request_id, "source_reg_number": player["reg_number"], "source_name": player["player_name"], "requested_name": request, "candidate_coach_id": candidate["coach_id"], "candidate_name": candidate["coach_name"], "match_method": method})
                row = {"request_id": request_id, "request_type": "coach", "source_reg_number": player["reg_number"], "source_name": player["player_name"], "requested_name": request, "status": status, "resolved_id": target, "resolved_name": matches[0][0]["coach_name"] if target else "", "candidate_count": str(len(matches)), "reason": reason}
                resolutions.append(row); self.coach_resolutions.append(row)
        write_csv(self.output_dir / "02_friend_request_candidates.csv", ["request_id", "source_reg_number", "source_name", "requested_name", "candidate_reg_number", "candidate_name", "match_method"], friend_candidates)
        write_csv(self.output_dir / "02_coach_request_candidates.csv", ["request_id", "source_reg_number", "source_name", "requested_name", "candidate_coach_id", "candidate_name", "match_method"], coach_candidates)
        write_csv(self.output_dir / "02_request_resolution.csv", ["request_id", "request_type", "source_reg_number", "source_name", "requested_name", "status", "resolved_id", "resolved_name", "candidate_count", "reason"], resolutions)

    def _load_overrides_and_relationships(self) -> None:
        self.constraints: list[dict[str, str]] = []
        team_ids = {f"{self.division}-{number:02d}" for number in range(1, self.config["team_counts"][self.division] + 1)}
        fixed_heads = self.config.get("fixed_team_heads", {}).get(self.division, {})
        fixed_co_heads = self.config.get("fixed_co_heads", {}).get(self.division, {})
        for team_id, coach_id in fixed_heads.items():
            self.constraints.append({"constraint_type": "assign_head_coach", "subject_id": team_id, "target_id": coach_id, "value": "config", "notes": "2026 fixed head", "status": "valid" if coach_id in self.coach_by_id else "invalid", "detail": ""})
        for team_id, coach_id in fixed_co_heads.items():
            self.constraints.append({"constraint_type": "assign_co_head_coach", "subject_id": team_id, "target_id": coach_id, "value": "config", "notes": "2026 confirmed co-head pairing", "status": "valid" if coach_id in self.coach_by_id else "invalid", "detail": ""})
        if self.overrides_path and self.overrides_path.exists():
            for raw in read_csv(self.overrides_path):
                typ, sub, target = clean(raw.get("constraint_type")), clean(raw.get("subject_id")), clean(raw.get("target_id"))
                if typ == "assign_practice_slot" and target not in self.slot_by_id:
                    # Accept the human form "Monday Early Longfellow Aux" (also "|" or "," separated).
                    wanted = key(re.sub(r"[|,]", " ", target))
                    matches = [sl["slot_id"] for sl in self.slots if key(f"{sl['day']} {sl['time']} {sl['location']}") == wanted]
                    if len(matches) == 1: target = matches[0]
                valid = typ in {"force_friend", "separate_players", "assign_head_coach", "assign_co_head_coach", "assign_practice_slot", "link_coach_daughter"}
                if typ in {"force_friend", "separate_players"}: valid = valid and sub in self.player_by_id and target in self.player_by_id and sub != target
                elif typ in {"assign_head_coach", "assign_co_head_coach"}: valid = valid and sub in team_ids and target in self.coach_by_id
                elif typ == "link_coach_daughter": valid = valid and sub in self.coach_by_id and target in self.player_by_id
                elif typ == "assign_practice_slot": valid = valid and sub in team_ids and target in self.slot_by_id
                self.constraints.append({"constraint_type": typ, "subject_id": sub, "target_id": target, "value": clean(raw.get("value")), "notes": clean(raw.get("notes")), "status": "valid" if valid else "invalid", "detail": "validated against normalized division records"})
        # Contradictory hard constraints are retained for review but never applied.
        for typ in ("assign_head_coach", "assign_practice_slot"):
            by_team: dict[str, set[str]] = defaultdict(set)
            for constraint in self.constraints:
                if constraint["constraint_type"] == typ and constraint["status"] == "valid":
                    by_team[constraint["subject_id"]].add(constraint["target_id"])
            for constraint in self.constraints:
                if constraint["constraint_type"] == typ and len(by_team[constraint["subject_id"]]) > 1:
                    constraint["status"] = "invalid"
                    constraint["detail"] = "conflicts with another assignment constraint for this team"
        valid_force = {frozenset((c["subject_id"], c["target_id"])) for c in self.constraints if c["constraint_type"] == "force_friend" and c["status"] == "valid"}
        valid_separate = {frozenset((c["subject_id"], c["target_id"])) for c in self.constraints if c["constraint_type"] == "separate_players" and c["status"] == "valid"}
        for constraint in self.constraints:
            pair = frozenset((constraint["subject_id"], constraint["target_id"]))
            if constraint["status"] == "valid" and pair in valid_force & valid_separate:
                constraint["status"] = "invalid"
                constraint["detail"] = "conflicts with force_friend/separate_players constraint"
        coach_team: dict[str, str] = {c["target_id"]: c["subject_id"] for c in self.constraints if c["constraint_type"] == "assign_head_coach" and c["status"] == "valid"}
        for constraint in self.constraints:
            if constraint["constraint_type"] == "assign_co_head_coach" and constraint["status"] == "valid":
                owner = coach_team.setdefault(constraint["target_id"], constraint["subject_id"])
                if owner != constraint["subject_id"]:
                    constraint["status"] = "invalid"
                    constraint["detail"] = f"coach is already assigned to {owner}"
        head_assignments = {
            (constraint["subject_id"], constraint["target_id"])
            for constraint in self.constraints
            if constraint["constraint_type"] == "assign_head_coach" and constraint["status"] == "valid"
        }
        for constraint in self.constraints:
            if (
                constraint["constraint_type"] == "assign_co_head_coach"
                and constraint["status"] == "valid"
                and (constraint["subject_id"], constraint["target_id"]) in head_assignments
            ):
                constraint["status"] = "invalid"
                constraint["detail"] = "duplicates this team's head coach"
        self.head_override = {c["subject_id"]: c["target_id"] for c in self.constraints if c["constraint_type"] == "assign_head_coach" and c["value"] != "config" and c["status"] == "valid"}
        self.co_head_override: dict[str, list[str]] = defaultdict(list)
        for c in self.constraints:
            if c["constraint_type"] == "assign_co_head_coach" and c["value"] != "config" and c["status"] == "valid" and c["target_id"] not in self.co_head_override[c["subject_id"]]:
                self.co_head_override[c["subject_id"]].append(c["target_id"])
        self.slot_override = {c["subject_id"]: c["target_id"] for c in self.constraints if c["constraint_type"] == "assign_practice_slot" and c["status"] == "valid"}
        self.coach_daughter_links = {(c["subject_id"], c["target_id"]) for c in self.constraints if c["constraint_type"] == "link_coach_daughter" and c["status"] == "valid"}
        self.force_pairs = {(c["subject_id"], c["target_id"]) for c in self.constraints if c["constraint_type"] == "force_friend" and c["status"] == "valid"}
        self.separate_pairs = {frozenset((c["subject_id"], c["target_id"])) for c in self.constraints if c["constraint_type"] == "separate_players" and c["status"] == "valid"}
        friend_rows = [{"source_reg_number": r["source_reg_number"], "target_reg_number": r["resolved_id"], "request_id": r["request_id"], "status": r["status"]} for r in self.friend_resolutions]
        coach_rows = [{"source_reg_number": r["source_reg_number"], "coach_id": r["resolved_id"], "request_id": r["request_id"], "status": r["status"]} for r in self.coach_resolutions]
        write_csv(self.output_dir / "03_friend_relationships.csv", ["source_reg_number", "target_reg_number", "request_id", "status"], friend_rows)
        write_csv(self.output_dir / "03_coach_relationships.csv", ["source_reg_number", "coach_id", "request_id", "status"], coach_rows)
        write_csv(self.output_dir / "03_constraints.csv", ["constraint_type", "subject_id", "target_id", "value", "notes", "status", "detail"], self.constraints)

    def _diagnose(self) -> None:
        friend_analysis = []
        unresolved_friend_requests = []
        for resolution in self.friend_resolutions:
            player = self.player_by_id[resolution["source_reg_number"]]
            action = {
                "resolved": "No outreach needed; continue to feasibility review.",
                "ambiguous": "Contact household to identify the requested player.",
                "unresolved": "Contact household to clarify the requested player.",
            }[resolution["status"]]
            friend_analysis.append({
                "request_id": resolution["request_id"],
                "reg_number": player["reg_number"],
                "player_name": player["player_name"],
                "submitter_name": player["submitter_name"],
                "submitter_email": player["submitter_email"],
                "requested_name": resolution["requested_name"],
                "resolution_status": resolution["status"],
                "resolved_id": resolution["resolved_id"],
                "resolved_name": resolution["resolved_name"],
                "candidate_count": resolution["candidate_count"],
                "recommended_action": action,
            })
            if resolution["status"] == "unresolved":
                unresolved_friend_requests.append({
                    "request_id": resolution["request_id"],
                    "reg_number": player["reg_number"],
                    "player_name": player["player_name"],
                    "submitter_name": player["submitter_name"],
                    "submitter_email": player["submitter_email"],
                    "requested_name": resolution["requested_name"],
                })
        feasibility = []
        impossible = []
        impossible_clusters = []
        same_coach_conflicts = []
        requested_coaches = {
            resolution["source_reg_number"]: resolution["resolved_id"]
            for resolution in self.coach_resolutions
            if resolution["status"] == "resolved"
        }
        for r in self.friend_resolutions:
            if r["status"] == "resolved":
                a, b = self.player_by_id[r["source_reg_number"]], self.player_by_id[r["resolved_id"]]
                shared = [s for s in self.slots if slot_ok(a, s) and slot_ok(b, s)]
                shared_slots = [s["slot_id"] for s in shared]
                feasibility.append({"request_id": r["request_id"], "source_reg_number": r["source_reg_number"], "target_reg_number": r["resolved_id"], "status": "feasible" if shared_slots else "infeasible", "shared_slot_ids": ";".join(shared_slots), "detail": "shared available slot exists" if shared_slots else "no shared practice slot"})
                if not shared_slots:
                    details = {"request_id": r["request_id"], "source_reg_number": a["reg_number"], "source_player_name": a["player_name"], "source_submitter_email": a["submitter_email"], "target_reg_number": b["reg_number"], "target_player_name": b["player_name"], "target_submitter_email": b["submitter_email"], "source_availability": availability_pattern(a), "target_availability": availability_pattern(b), "shared_slot_ids": "", "detail": "The players have no practice slot in common."}
                    impossible.append(details)
                    if requested_coaches.get(a["reg_number"]) and requested_coaches.get(a["reg_number"]) == requested_coaches.get(b["reg_number"]):
                        same_coach_conflicts.append({**details, "coach_id": requested_coaches[a["reg_number"]], "coach_name": self.coach_by_id.get(requested_coaches[a["reg_number"]], {}).get("coach_name", "")})
        for cluster_number, member_ids in enumerate(self._friend_components(), 1):
            shared_slots = [
                slot["slot_id"]
                for slot in self.slots
                if all(slot_ok(self.player_by_id[member_id], slot) for member_id in member_ids)
            ]
            if shared_slots:
                continue
            members = [self.player_by_id[member_id] for member_id in member_ids]
            member_names = "; ".join(member["player_name"] for member in members)
            for member in members:
                impossible_clusters.append({
                    "cluster_id": f"cluster-{cluster_number:02d}",
                    "cluster_size": str(len(members)),
                    "reg_number": member["reg_number"],
                    "player_name": member["player_name"],
                    "submitter_name": member["submitter_name"],
                    "submitter_email": member["submitter_email"],
                    "availability": availability_pattern(member),
                    "cluster_members": member_names,
                    "shared_slot_ids": "",
                    "reason": "NO_COMMON_PRACTICE_SLOT_FOR_CLUSTER",
                    "detail": "No single practice slot can accommodate every resolved friend in this cluster.",
                    "recommended_action": "Contact cluster households to revise friend requests or availability during registration.",
                })
        counts = len(self.players); teams = self.config["team_counts"][self.division]; roster = self.config["roster"]
        health = [{"division": self.division, "registered_players": str(counts), "configured_teams": str(teams), "minimum_capacity": str(teams * roster["minimum"]), "target_capacity": str(teams * roster["target"]), "maximum_capacity": str(teams * roster["maximum"]), "status": "feasible" if teams * roster["minimum"] <= counts <= teams * roster["maximum"] else "infeasible"}]
        write_csv(self.output_dir / "04_friend_request_analysis.csv", ["request_id", "reg_number", "player_name", "submitter_name", "submitter_email", "requested_name", "resolution_status", "resolved_id", "resolved_name", "candidate_count", "recommended_action"], friend_analysis)
        write_csv(self.output_dir / "04_unresolved_friend_requests.csv", ["request_id", "reg_number", "player_name", "submitter_name", "submitter_email", "requested_name"], unresolved_friend_requests)
        write_csv(self.output_dir / "04_friendship_feasibility.csv", ["request_id", "source_reg_number", "target_reg_number", "status", "shared_slot_ids", "detail"], feasibility)
        write_csv(self.output_dir / "04_impossible_friend_requests.csv", ["request_id", "source_reg_number", "source_player_name", "source_submitter_email", "target_reg_number", "target_player_name", "target_submitter_email", "source_availability", "target_availability", "shared_slot_ids", "detail"], impossible)
        write_csv(self.output_dir / "04_impossible_friend_clusters.csv", ["cluster_id", "cluster_size", "reg_number", "player_name", "submitter_name", "submitter_email", "availability", "cluster_members", "shared_slot_ids", "reason", "detail", "recommended_action"], impossible_clusters)
        write_csv(self.output_dir / "04_friend_coach_conflicts.csv", ["request_id", "source_reg_number", "source_player_name", "source_submitter_email", "target_reg_number", "target_player_name", "target_submitter_email", "coach_id", "coach_name", "source_availability", "target_availability", "shared_slot_ids", "detail"], same_coach_conflicts)
        write_csv(self.output_dir / "04_registration_health.csv", list(health[0]), health)

    @staticmethod
    def team_coach_ids(team: dict[str, str]) -> set[str]:
        return {x for x in (team["head_coach_id"], team["co_head_coach_id"], *team.get("extra_coach_ids", "").split(";")) if x}

    def _friend_components(self) -> list[list[str]]:
        adjacency: dict[str, set[str]] = defaultdict(set)
        for r in self.friend_resolutions:
            a, b = r["source_reg_number"], r["resolved_id"]
            if r["status"] == "resolved" and a in self.player_by_id and b in self.player_by_id and a != b:
                adjacency[a].add(b); adjacency[b].add(a)
        seen: set[str] = set(); components = []
        for start in sorted(adjacency):
            if start in seen: continue
            stack, members = [start], set()
            while stack:
                node = stack.pop()
                if node in members: continue
                members.add(node); stack.extend(adjacency[node] - members)
            seen |= members; components.append(sorted(members))
        return sorted(components, key=lambda c: (-len(c), c[0]))

    def _build_anchor_groups(self, heads: dict[str, str], co_heads: dict[str, list[str]]) -> None:
        """Coach -> daughter -> friend groups, keyed by team. Contains PII only in memory and private reports."""
        directed: dict[str, set[str]] = defaultdict(set)
        for r in self.friend_resolutions:
            if r["status"] == "resolved": directed[r["source_reg_number"]].add(r["resolved_id"])
        by_email: dict[str, list[str]] = defaultdict(list)
        for p in self.players:
            if p["submitter_email"]: by_email[p["submitter_email"].lower()].append(p["reg_number"])
        by_name: dict[str, list[str]] = defaultdict(list)
        for p in self.players: by_name[key(p["player_name"])].append(p["reg_number"])
        override_links: dict[str, set[str]] = defaultdict(set)
        for cid, pid in self.coach_daughter_links: override_links[cid].add(pid)
        child_links: dict[str, tuple[str, set[str]]] = {}
        link_rows = []
        for coach in self.coaches:
            if coach["division"] != self.division or (key(coach["registered_child"]) != "yes" and not coach.get("child_name") and coach["coach_id"] not in override_links): continue
            cid, child = coach["coach_id"], coach.get("child_name", "")
            if cid in override_links: method, found, status = "override", set(override_links[cid]), "linked"
            elif key(re.sub(r"\(.*?\)", " ", child)):
                matches = by_name.get(key(re.sub(r"\(.*?\)", " ", child)), [])
                method, found = "child_name", set(matches) if len(matches) == 1 else set()
                status = "linked" if len(matches) == 1 else "ambiguous" if matches else "no_match"
            elif key(coach["registered_child"]) == "yes" and coach.get("coach_email"):
                found = set(by_email.get(coach["coach_email"], []))
                method, status = "email", "linked" if found else "no_match"
            else: method, found, status = "none", set(), "no_match"
            child_links[cid] = (method, found)
            link_rows.append({"coach_id": cid, "coach_name": coach["coach_name"], "child_name": child, "method": method, "status": status, "matched_reg_numbers": ";".join(sorted(found))})
        write_csv(self.output_dir / "05_coach_child_links.csv", ["coach_id", "coach_name", "child_name", "method", "status", "matched_reg_numbers"], sorted(link_rows, key=lambda r: (r["status"] == "linked", r["coach_name"])))
        self.anchor_groups: dict[str, list[dict[str, Any]]] = {}
        for team_id in sorted(set(heads) | set(co_heads)):
            groups = []
            for coach_id in (heads.get(team_id), *co_heads.get(team_id, [])):
                coach = self.coach_by_id.get(coach_id or "")
                if not coach: continue
                daughters = set(child_links.get(coach_id or "", ("", set()))[1])
                for daughter in sorted(daughters):
                    friends = {}
                    for target in directed[daughter]: friends[target] = "mutual" if daughter in directed[target] else "requested_by_daughter"
                    for source, targets in directed.items():
                        if daughter in targets and source not in friends: friends[source] = "requested_daughter"
                    ordered = sorted(friends.items(), key=lambda item: ({"mutual": 0, "requested_by_daughter": 1}.get(item[1], 2), item[0]))
                    groups.append({"coach_id": coach_id, "daughter": daughter, "friends": ordered})
            if groups: self.anchor_groups[team_id] = groups

    def _prepare_teams(self) -> None:
        count = self.config["team_counts"][self.division]
        configured = self.config.get("fixed_team_heads", {}).get(self.division, {})
        heads = dict(configured)
        for team, coach in self.head_override.items(): heads[team] = coach
        candidate_heads = [c["coach_id"] for c in self.coaches if key(c["role"]) == "head coach" and c["coach_id"] not in heads.values()]
        for n in range(1, count + 1):
            team_id = f"{self.division}-{n:02d}"
            if team_id not in heads and candidate_heads: heads[team_id] = candidate_heads.pop(0)
        self.teams: list[dict[str, str]] = []
        assignment_issues: list[dict[str, str]] = []
        co_heads: dict[str, list[str]] = {team: [coach] for team, coach in self.config.get("fixed_co_heads", {}).get(self.division, {}).items()}
        for team, extra in self.co_head_override.items():
            co_heads[team] = [*co_heads.get(team, []), *[c for c in extra if c not in co_heads.get(team, [])]]
        for n in range(1, count + 1):
            team_id = f"{self.division}-{n:02d}"; head_id = heads.get(team_id, ""); co_list = co_heads.get(team_id, []); co_head_id = co_list[0] if co_list else ""
            if head_id not in self.coach_by_id:
                assignment_issues.append({"team_id": team_id, "severity": "error", "issue": "no valid head coach", "detail": "configured count retained but no compatible coach can be assigned"})
            for extra_id in co_list:
                if extra_id not in self.coach_by_id:
                    assignment_issues.append({"team_id": team_id, "severity": "error", "issue": "invalid co-head coach", "detail": extra_id})
            self.teams.append({"team_id": team_id, "division": self.division, "head_coach_id": head_id, "co_head_coach_id": co_head_id, "extra_coach_ids": ";".join(co_list[1:]), "slot_id": "", "day": "", "time": "", "location": ""})
        self._build_anchor_groups(heads, co_heads)
        self.friend_components = self._friend_components()
        coach_requests = [(r["source_reg_number"], r["resolved_id"]) for r in self.coach_resolutions if r["status"] == "resolved"]
        forced_neighbors: dict[str, set[str]] = defaultdict(set)
        for first, second in self.force_pairs:
            forced_neighbors[first].add(second)
            forced_neighbors[second].add(first)

        def expand_forced_members(member_ids: set[str]) -> set[str]:
            expanded = set(member_ids)
            pending = list(member_ids)
            while pending:
                member = pending.pop()
                for forced in forced_neighbors[member] - expanded:
                    expanded.add(forced)
                    pending.append(forced)
            return expanded

        self._open_slots = lambda player: sum((player[f"{d}_early"] == "yes") + (player[f"{d}_late"] == "yes") for d in DAYS)
        friends_of: dict[str, set[str]] = defaultdict(set)
        self._directed_friends: dict[str, set[str]] = defaultdict(set)
        for r in self.friend_resolutions:
            if r["status"] == "resolved" and r["resolved_id"] in self.player_by_id and r["source_reg_number"] in self.player_by_id:
                friends_of[r["source_reg_number"]].add(r["resolved_id"]); friends_of[r["resolved_id"]].add(r["source_reg_number"])
            if r["status"] == "resolved": self._directed_friends[r["source_reg_number"]].add(r["resolved_id"])
        self.mutual_friend_pairs = {
            frozenset((source, target))
            for source, targets in self._directed_friends.items()
            for target in targets
            if source in self._directed_friends.get(target, set())
        }
        relationship_ids = {
            player["reg_number"]
            for player in self.players
            if player["friend_requests"] or player["coach_request"]
        }
        relationship_ids |= {
            player_id
            for resolution in self.friend_resolutions
            if resolution["status"] == "resolved"
            for player_id in (resolution["source_reg_number"], resolution["resolved_id"])
        }
        relationship_ids |= {
            group_member
            for groups in self.anchor_groups.values()
            for group in groups
            for group_member in (group["daughter"], *(friend for friend, _ in group["friends"]))
        }
        self.low_availability_yield_ids = {
            player["reg_number"]
            for player in self.players
            if self._open_slots(player) < LOW_AVAILABILITY_SLOTS
            and player["reg_number"] not in relationship_ids
        }
        self.team_info: dict[str, dict[str, Any]] = {}
        for team in self.teams:
            tid = team["team_id"]
            coaches = [self.coach_by_id[x] for x in (team["head_coach_id"], team["co_head_coach_id"]) if x in self.coach_by_id]
            # An assistant with a linked daughter must also be able to attend, so the daughter can be placed.
            daughter_coaches = {g["coach_id"] for g in self.anchor_groups.get(tid, [])}
            extras = [self.coach_by_id[x] for x in team["extra_coach_ids"].split(";") if x in self.coach_by_id]
            required = coaches + [c for c in extras if c["coach_id"] in daughter_coaches]
            full_ids = {s["slot_id"] for s in self.slots if coaches and all(slot_ok(c, s) for c in required)}
            # Slots suiting the head and co-head stay candidates so a coach's daughter can outrank an assistant's schedule.
            valid = [s for s in self.slots if coaches and all(slot_ok(c, s) for c in coaches)]
            daughters = [self.player_by_id[g["daughter"]] for g in self.anchor_groups.get(tid, []) if g["daughter"] in self.player_by_id]
            anchor_ids = {
                pid
                for group in self.anchor_groups.get(tid, [])
                for pid in (group["daughter"], *(friend for friend, _ in group["friends"]))
                if pid in self.player_by_id
            }
            locked_anchor_ids_by_slot: dict[str, set[str]] = {}
            if anchor_ids:
                protected_ids = expand_forced_members(anchor_ids)
                if len(protected_ids) > self.config["roster"]["maximum"]:
                    assignment_issues.append({"team_id": tid, "severity": "warning", "issue": "coach child friend cluster exceeds roster maximum", "detail": f"{len(protected_ids)} protected players exceed the {self.config['roster']['maximum']}-player limit"})
                elif any(frozenset(pair) in self.separate_pairs for pair in combinations(sorted(protected_ids), 2)):
                    assignment_issues.append({"team_id": tid, "severity": "warning", "issue": "coach child friend cluster conflicts with separation constraint", "detail": "explicit player separation prevents an early cluster lock"})
                else:
                    daughter_ids = {group["daughter"] for group in self.anchor_groups.get(tid, [])}
                    cluster_slots = {
                        slot["slot_id"]: {pid for pid in protected_ids if slot_ok(self.player_by_id[pid], slot)}
                        for slot in valid
                        if all(slot_ok(self.player_by_id[pid], slot) for pid in daughter_ids)
                    }
                    if cluster_slots:
                        largest_cluster = max(len(member_ids) for member_ids in cluster_slots.values())
                        locked_anchor_ids_by_slot = {
                            slot_id: member_ids
                            for slot_id, member_ids in cluster_slots.items()
                            if len(member_ids) == largest_cluster
                        }
                        valid = [slot for slot in valid if slot["slot_id"] in locked_anchor_ids_by_slot]
                    else:
                        assignment_issues.append({"team_id": tid, "severity": "warning", "issue": "coach child friend cluster has no compatible child practice slot", "detail": "no coach-compatible slot fits every linked child"})
            team_coach_ids = self.team_coach_ids(team)
            requesters = [self.player_by_id[pid] for pid, cid in coach_requests if cid in team_coach_ids and pid in self.player_by_id]
            cluster_weight: dict[str, int] = {}
            anchor_member_ids = [pid for g in self.anchor_groups.get(tid, []) for pid in [g["daughter"], *[f for f, _ in g["friends"]]] if pid in self.player_by_id]
            for pid in anchor_member_ids: cluster_weight[pid] = 1
            for pl in requesters: cluster_weight[pl["reg_number"]] = 2
            for g in self.anchor_groups.get(tid, []): cluster_weight[g["daughter"]] = 3
            preferred = self.slot_override.get(tid)
            if preferred: valid = [s for s in valid if s["slot_id"] == preferred]
            self.team_info[tid] = {
                "coaches": coaches, "extras": extras, "full_ids": full_ids, "valid": valid, "daughters": daughters, "cluster_weight": cluster_weight,
                "protect_order": sorted((pl for pl in requesters if self._open_slots(pl) < LOW_AVAILABILITY_SLOTS), key=lambda pl: (self._open_slots(pl), pl["reg_number"])),
                "locked_anchor_ids_by_slot": locked_anchor_ids_by_slot,
                "anchor_size": sum(1 + len(g["friends"]) for g in self.anchor_groups.get(tid, [])),
            }
        self.slot_issues = assignment_issues

    def slot_vector(self, team: dict[str, str], slot: dict[str, str], chosen: list[dict[str, str]], pending: list[dict[str, str]], free_ids: set[str]) -> tuple[Any, ...]:
        """Rank vector (higher is better) for a team using a slot, given the division's other chosen slots and still-pending teams."""
        info = self.team_info[team["team_id"]]
        coaches, extras, daughters = info["coaches"], info["extras"], info["daughters"]
        player_count = sum(slot_ok(p, slot) for p in self.players)
        # Do not pick a slot that would leave a late-only (or early-only) player with no schedule among the remaining teams.
        future_slots = free_ids - {slot["slot_id"]}
        coverable = 0
        for player in self.players:
            if player["reg_number"] in self.low_availability_yield_ids:
                continue
            covered = slot_ok(player, slot) or any(slot_ok(player, c) for c in chosen)
            if not covered:
                for future in pending:
                    future_coaches = self.team_info[future["team_id"]]["coaches"]
                    forced = self.slot_override.get(future["team_id"])
                    if any(candidate["slot_id"] in future_slots and (not forced or candidate["slot_id"] == forced) and future_coaches
                           and all(slot_ok(coach, candidate) for coach in future_coaches) and slot_ok(player, candidate) for candidate in self.slots):
                        covered = True
                        break
            coverable += covered
        coach_cover = sum(slot_ok(c, slot) for c in [*coaches, *extras])
        spare = self.config["roster"]["maximum"] - info["anchor_size"]
        seed_cover = max((min(sum(slot_ok(self.player_by_id[pid], slot) for pid in comp), spare) for comp in self.friend_components if len(comp) >= 3), default=0) if info["anchor_size"] < SEED_TARGET else 0
        # Coach cluster: daughters (3), coach requesters (2), friends of either (1); ties favor keeping low-availability players.
        attending = {pid: w for pid, w in info["cluster_weight"].items() if slot_ok(self.player_by_id[pid], slot)}
        satisfied_edges = sum(1 for a in attending for b in self._directed_friends.get(a, ()) if b in attending)
        # Requesters with fewer than 5 of 10 slots open have the fewest alternatives, so they are protected first.
        flags = [int(slot_ok(pl, slot)) for pl in info["protect_order"]]
        protected = 0
        for f in flags: protected = protected * 2 + f
        requester_cover = sum(attending.values()) + satisfied_edges
        anchor_cover = -sum(self._open_slots(self.player_by_id[pid]) for pid in attending)
        cluster_cover = sum(min(sum(slot_ok(self.player_by_id[pid], slot) for pid in comp), self.config["roster"]["maximum"]) ** 2 for comp in self.friend_components if len(comp) >= 3)
        mutual_pair_cover = sum(
            all(slot_ok(self.player_by_id[player_id], slot) for player_id in pair)
            and not any(all(slot_ok(self.player_by_id[player_id], selected_slot) for player_id in pair) for selected_slot in chosen)
            for pair in self.mutual_friend_pairs
        )
        score = player_count + (1 if slot["day"] == "Friday" and self.division in {"4th", "5th"} else 0)
        return (
            int(all(slot_ok(d, slot) for d in daughters)),
            int(slot["slot_id"] in info["full_ids"]),
            coach_cover,
            protected,
            requester_cover,
            anchor_cover,
            mutual_pair_cover,
            seed_cover,
            cluster_cover,
            coverable,
            score,
        )

    def _finish_slots(self, final_vectors: dict[str, dict[str, tuple[Any, ...]]]) -> None:
        """Write slot outputs once the joint solver has fixed every team's slot."""
        assignment_issues = self.slot_issues
        score_rows = []
        for team in self.teams:
            info = self.team_info[team["team_id"]]
            vectors = final_vectors.get(team["team_id"], {})
            if not team["slot_id"]:
                assignment_issues.append({"team_id": team["team_id"], "severity": "error", "issue": "no unique coach-compatible practice slot", "detail": "fixed coach availability, slot override and slots used by other teams were honored"})
                continue
            vector = vectors.get(team["slot_id"])
            if vector:
                if info["daughters"] and not vector[0]: assignment_issues.append({"team_id": team["team_id"], "severity": "warning", "issue": "no slot works for every coach's daughter", "detail": "slot chosen without requiring all linked daughters"})
                elif not vector[1]: assignment_issues.append({"team_id": team["team_id"], "severity": "warning", "issue": "slot chosen for head, co-head and daughters only", "detail": "no slot also suits every assistant coach with a linked daughter"})
            for slot_id, vec in sorted(vectors.items()):
                slot = self.slot_by_id[slot_id]
                score_rows.append({"team_id": team["team_id"], "slot_id": slot_id, "day": slot["day"], "time": slot["time"], "location": slot["location"], "available_players": str(sum(slot_ok(p, slot) for p in self.players)), "score": f"{vec[10]}; coverage={vec[9]}", "selected": "yes" if slot_id == team["slot_id"] else ""})
        self.coach_assignment_issues = assignment_issues
        write_csv(self.output_dir / "05_team_slots.csv", ["team_id", "division", "head_coach_id", "co_head_coach_id", "extra_coach_ids", "slot_id", "day", "time", "location"], self.teams)
        coach_rows = []
        for team in self.teams:
            extras = [x for x in team["extra_coach_ids"].split(";") if x]
            for extra in extras:
                if team["slot_id"] and extra in self.coach_by_id and not slot_ok(self.coach_by_id[extra], team):
                    assignment_issues.append({"team_id": team["team_id"], "severity": "warning", "issue": "additional coach unavailable for practice slot", "detail": f"{extra} {self.coach_by_id[extra]['coach_name']} cannot attend {team['day']} {team['time']}"})
            for role, cid in (("head", team["head_coach_id"]), ("co-head", team["co_head_coach_id"]), *[("assistant", x) for x in extras]):
                if cid:
                    coach_rows.append({"team_id": team["team_id"], "coach_id": cid, "coach_name": self.coach_by_id.get(cid, {}).get("coach_name", ""), "role": role, "slot_id": team["slot_id"], "day": team["day"], "time": team["time"], "location": team["location"]})
        write_csv(self.output_dir / "05_coach_assignments.csv", ["team_id", "coach_id", "coach_name", "role", "slot_id", "day", "time", "location"], coach_rows)
        write_csv(self.output_dir / "05_coach_assignment_scores.csv", ["team_id", "slot_id", "day", "time", "location", "available_players", "score", "selected"], score_rows)
        write_csv(self.output_dir / "05_coach_assignment_issues.csv", ["team_id", "severity", "issue", "detail"], assignment_issues)

    def _assign_players(self) -> None:
        roster = self.config["roster"]; feasibility = []; operational = [t for t in self.teams if t["slot_id"]]
        team_count = len(self.teams)
        base_size, remainder = divmod(len(self.players), team_count)
        if not roster["minimum"] <= base_size <= roster["maximum"] or (
            remainder and base_size + 1 > roster["maximum"]
        ):
            raise ValueError(
                f"{len(self.players)} players cannot be distributed across {team_count} teams "
                f"within the configured {roster['minimum']}-{roster['maximum']} roster range"
            )
        # Anchor groups may fill a team to the roster maximum; limits are rebalanced afterward.
        team_limits = {team["team_id"]: roster["maximum"] for team in self.teams}
        for player in self.players:
            options = [t for t in operational if slot_ok(player, t)]
            feasibility.append({"reg_number": player["reg_number"], "player_name": player["player_name"], "division": player["division"], "feasible_team_count": str(len(options)), "feasible_team_ids": ";".join(t["team_id"] for t in options), "status": "feasible" if options else "infeasible"})
        write_csv(self.output_dir / "06_player_team_feasibility.csv", ["reg_number", "player_name", "division", "feasible_team_count", "feasible_team_ids", "status"], feasibility)
        rosters: dict[str, list[str]] = {t["team_id"]: [] for t in self.teams}
        assigned: dict[str, str] = {}
        issues: list[dict[str, str]] = []
        # Hard forced-friend components are placed together only where all members are available.
        components: list[list[str]] = []; parent = {p["reg_number"]: p["reg_number"] for p in self.players}
        def find(x: str) -> str:
            while parent[x] != x: parent[x] = parent[parent[x]]; x = parent[x]
            return x
        for a, b in self.force_pairs:
            a, b = find(a), find(b)
            if a != b: parent[b] = a
        grouped: dict[str, list[str]] = defaultdict(list)
        for pid in parent: grouped[find(pid)].append(pid)
        components = sorted((sorted(v) for v in grouped.values()), key=lambda v: (len([t for t in operational if all(slot_ok(self.player_by_id[p], t) for p in v)]), -len(v), v))
        coach_pref = {r["source_reg_number"]: r["resolved_id"] for r in self.coach_resolutions if r["status"] == "resolved"}
        friends = defaultdict(set)
        directed_friends: dict[str, set[str]] = defaultdict(set)
        for r in self.friend_resolutions:
            if r["status"] == "resolved":
                friends[r["source_reg_number"]].add(r["resolved_id"]); friends[r["resolved_id"]].add(r["source_reg_number"])
                directed_friends[r["source_reg_number"]].add(r["resolved_id"])
        group_of = {pid: g for g in components for pid in g}
        anchor_rows = []
        by_team = {t["team_id"]: t for t in operational}
        def place_anchor(team: dict[str, str], pid: str, protected_group: list[str] | None = None) -> str:
            group = protected_group or group_of[pid]
            if any(p in assigned for p in group): return "on_team" if assigned.get(pid) == team["team_id"] else "already_placed_elsewhere"
            if not all(slot_ok(self.player_by_id[p], team) for p in group): return "no_shared_slot_with_coach"
            if len(rosters[team["team_id"]]) + len(group) > team_limits[team["team_id"]]: return "team_full"
            # Never let anchors leave the other teams unable to reach the roster minimum.
            needed = sum(max(len(rosters[t["team_id"]]) + (len(group) if t is team else 0), roster["minimum"]) for t in self.teams)
            if needed > len(self.players): return "team_full"
            if any(frozenset((a, b)) in self.separate_pairs for a in group for b in rosters[team["team_id"]]): return "separated"
            for p in group: rosters[team["team_id"]].append(p); assigned[p] = team["team_id"]
            return "on_team"

        locked_anchor_outcomes: dict[str, str] = {}
        locked_anchor_members: set[str] = set()
        for team_id, team in sorted(by_team.items()):
            protected = sorted(self.team_info[team_id]["locked_anchor_ids_by_slot"].get(team["slot_id"], set()))
            if not protected:
                continue
            outcome = place_anchor(team, protected[0], protected)
            locked_anchor_outcomes[team_id] = outcome
            if outcome == "on_team":
                locked_anchor_members.update(protected)

        for team_id, groups in sorted(self.anchor_groups.items()):
            team = by_team.get(team_id)
            for g in groups:
                daughter = self.player_by_id[g["daughter"]]
                locked_ids = self.team_info.get(team_id, {}).get("locked_anchor_ids_by_slot", {}).get(team.get("slot_id", "") if team else "", set())
                locked = g["daughter"] in locked_ids
                status = locked_anchor_outcomes[team_id] if locked and team_id in locked_anchor_outcomes else place_anchor(team, g["daughter"]) if team else "team_has_no_slot"
                anchor_rows.append({"team_id": team_id, "coach_id": g["coach_id"], "daughter_reg_number": g["daughter"], "daughter_name": daughter["player_name"], "member_reg_number": g["daughter"], "member_name": daughter["player_name"], "relationship": "daughter", "outcome": status})
                if status != "on_team": continue
                for fid, relation in g["friends"]:
                    friend = self.player_by_id[fid]
                    result = locked_anchor_outcomes[team_id] if fid in locked_ids and team_id in locked_anchor_outcomes else place_anchor(team, fid)
                    anchor_rows.append({"team_id": team_id, "coach_id": g["coach_id"], "daughter_reg_number": g["daughter"], "daughter_name": daughter["player_name"], "member_reg_number": fid, "member_name": friend["player_name"], "relationship": relation, "outcome": result})
        self.anchored_team_count = len({r["team_id"] for r in anchor_rows if r["relationship"] == "daughter" and r["outcome"] == "on_team"})
        write_csv(self.output_dir / "05_anchor_teams.csv", ["team_id", "coach_id", "daughter_reg_number", "daughter_name", "member_reg_number", "member_name", "relationship", "outcome"], anchor_rows)
        # Rebalance remaining capacity: grow the smallest teams first so sizes stay as even as possible.
        sizes = {t["team_id"]: len(rosters[t["team_id"]]) for t in self.teams}
        for _ in range(len(self.players) - len(assigned)):
            smallest = min((t for t in sizes if sizes[t] < roster["maximum"]), key=lambda t: (sizes[t], t), default=None)
            if smallest is None: break
            sizes[smallest] += 1
        team_limits.update(sizes)
        # Players requesting a resolved head/co-head coach are placed before others, so
        # flexible-availability players are not crowded out of the coach's team.
        def coach_priority(group: list[str]) -> int:
            wanted = {coach_pref[p] for p in group if p in coach_pref}
            return 0 if any(wanted & self.team_coach_ids(t) and all(slot_ok(self.player_by_id[p], t) for p in group) for t in operational) else 1
        components = sorted(components, key=lambda g: (coach_priority(g),))
        stage_of: dict[str, str] = {}
        for row in anchor_rows:
            if row["outcome"] == "on_team" and row["member_reg_number"] in assigned: stage_of[row["member_reg_number"]] = "anchor"
        for pid in locked_anchor_members:
            stage_of[pid] = "anchor"

        def blocked(team_id: str, ids: set[str]) -> bool:
            return any(frozenset((a, b)) in self.separate_pairs for a in ids for b in rosters[team_id])

        def place_group(group: list[str], stage: str, report: bool, preserve_remaining_feasibility: bool = False) -> bool:
            if any(p in assigned for p in group): return False
            options = [t for t in operational if len(rosters[t["team_id"]]) + len(group) <= team_limits[t["team_id"]] and all(slot_ok(self.player_by_id[p], t) for p in group) and not blocked(t["team_id"], set(group))]
            def score(team: dict[str, str]) -> tuple[int, int, int, str]:
                team_ids = set(rosters[team["team_id"]])
                coach_score = sum(coach_pref.get(p) in self.team_coach_ids(team) for p in group)
                friend_score = sum(f in team_ids for p in group for f in friends[p])
                return (coach_score, friend_score, -len(team_ids), team["team_id"])
            if not options:
                if report:
                    for pid in group: issues.append({"reg_number": pid, "severity": "error", "issue": "no feasible team", "detail": "availability, capacity, or separation constraint"})
                return False
            choices = sorted(options, key=lambda t: (-score(t)[0], -score(t)[1], -score(t)[2], score(t)[3]))
            feasibility_passes = (False, True) if preserve_remaining_feasibility else (True,)
            for allow_low_availability_tradeoff in feasibility_passes:
                for choice in choices:
                    for pid in group: rosters[choice["team_id"]].append(pid); assigned[pid] = choice["team_id"]; stage_of[pid] = stage
                    if (
                        not preserve_remaining_feasibility
                        or everyone_still_fits(allow_low_availability_tradeoff)
                    ):
                        return True
                    for pid in group:
                        rosters[choice["team_id"]].remove(pid)
                        del assigned[pid]
                        del stage_of[pid]
            return False

        def everyone_still_fits(allow_low_availability_tradeoff: bool = False) -> bool:
            """Matching check: every unplaced player keeps a slot-compatible team with room."""
            open_players = [
                p["reg_number"]
                for p in self.players
                if p["reg_number"] not in assigned
                and (
                    not allow_low_availability_tradeoff
                    or p["reg_number"] not in self.low_availability_yield_ids
                )
            ]
            room = {t["team_id"]: team_limits[t["team_id"]] - len(rosters[t["team_id"]]) for t in operational}
            holders: dict[str, list[str]] = {t: [] for t in room}
            def try_place(pid: str, seen: set[str]) -> bool:
                for t in operational:
                    tid = t["team_id"]
                    if tid in seen or not slot_ok(self.player_by_id[pid], t): continue
                    seen.add(tid)
                    if len(holders[tid]) < room[tid]:
                        holders[tid].append(pid); return True
                    for other in list(holders[tid]):
                        if try_place(other, seen):
                            holders[tid].remove(other); holders[tid].append(pid); return True
                return False
            return all(try_place(pid, set()) for pid in sorted(open_players, key=lambda x: sum(slot_ok(self.player_by_id[x], t) for t in operational)))

        for group in [g for g in components if coach_priority(g) == 0]:
            place_group(group, "coach_request", False, preserve_remaining_feasibility=True)

        mutual_adjacency: dict[str, set[str]] = defaultdict(set)
        for source, targets in directed_friends.items():
            for target in targets:
                if source in directed_friends.get(target, set()):
                    mutual_adjacency[source].add(target)
                    mutual_adjacency[target].add(source)
        mutual_groups: list[list[str]] = []
        seen_mutual: set[str] = set()
        for start in sorted(mutual_adjacency):
            if start in seen_mutual:
                continue
            pending, members = [start], set()
            while pending:
                member = pending.pop()
                if member in members:
                    continue
                members.add(member)
                pending.extend(mutual_adjacency[member] - members)
            seen_mutual |= members
            protected = sorted({pid for member in members for pid in group_of[member]})
            if len(protected) > 1:
                mutual_groups.append(protected)
        for group in sorted(mutual_groups, key=lambda g: (len([t for t in operational if all(slot_ok(self.player_by_id[p], t) for p in g)]), -len(g), g)):
            place_group(group, "mutual_pair", False, preserve_remaining_feasibility=True)

        weights: dict[frozenset, int] = defaultdict(int)
        for a_id, targets in directed_friends.items():
            for b_id in targets:
                if a_id in self.player_by_id and b_id in self.player_by_id and a_id != b_id: weights[frozenset((a_id, b_id))] += 1
        unit_of = {pid: tuple(group) for group in components for pid in group}
        cluster_rows: list[dict[str, str]] = []

        def objective(local: dict[tuple[str, ...], str], edges: list[tuple[str, str]]) -> tuple[int, int, int]:
            def team_of(pid: str) -> str:
                return assigned.get(pid) or local.get(unit_of[pid], "")
            satisfied_edges = [
                (source, target)
                for source, target in edges
                if team_of(source) and team_of(source) == team_of(target)
            ]
            satisfied_requesters = len({source for source, _ in satisfied_edges})
            friend_total = len(satisfied_edges)
            coach_total = 0
            for unit, team_id in local.items():
                team = by_team[team_id]
                coach_total += sum(coach_pref.get(pid) in self.team_coach_ids(team) for pid in unit)
            return (coach_total, satisfied_requesters, friend_total)

        def fits(unit: tuple[str, ...], team: dict[str, str], local: dict[tuple[str, ...], str]) -> bool:
            tid = team["team_id"]
            mates = [pid for u, t in local.items() if t == tid and u != unit for pid in u]
            if len(rosters[tid]) + len(mates) + len(unit) > team_limits[tid]: return False
            if not all(slot_ok(self.player_by_id[pid], team) for pid in unit): return False
            return not any(frozenset((x, y)) in self.separate_pairs for x in unit for y in [*rosters[tid], *mates])

        for cluster_number, comp in enumerate(self.friend_components, 1):
            units = sorted({unit_of[pid] for pid in comp if pid not in assigned}, key=lambda u: (-sum(w for pair, w in weights.items() if pair & set(u)), u))
            if not units: continue
            members = set(comp)
            edges = [
                (source, target)
                for source in sorted(members)
                for target in sorted(directed_friends.get(source, ()))
                if target in members
            ]
            local: dict[tuple[str, ...], str] = {}
            for unit in units:
                choices = [t for t in operational if fits(unit, t, local)]
                if not choices: continue
                def build_key(t: dict[str, str]) -> tuple[Any, ...]:
                    trial = {**local, unit: t["team_id"]}
                    able = sum(slot_ok(self.player_by_id[pid], t) for u in units if u not in local and u != unit for pid in u)
                    room_first = len(rosters[t["team_id"]]) if len(comp) >= 5 else 0
                    return (tuple(-x for x in objective(trial, edges)), room_first, -able, len(rosters[t["team_id"]]), t["team_id"])
                local[unit] = min(choices, key=build_key)["team_id"]
            improved = True
            while improved:
                improved = False
                current = objective(local, edges)
                for unit in units:
                    for t in operational:
                        old = local.get(unit)
                        if t["team_id"] == old: continue
                        trial = {k: v for k, v in local.items() if k != unit}
                        if not fits(unit, t, trial): continue
                        trial[unit] = t["team_id"]
                        if objective(trial, edges) > current: local, current, improved = trial, objective(trial, edges), True
                for first, second in combinations([u for u in units if u in local], 2):
                    if local[first] == local[second]: continue
                    trial = {**local, first: local[second], second: local[first]}
                    ok_swap = all(fits(u, by_team[trial[u]], {k: v for k, v in trial.items() if k != u}) for u in (first, second))
                    if ok_swap and objective(trial, edges) > current: local, current, improved = trial, objective(trial, edges), True
            for unit in units:
                if unit not in local: continue
                preferred = local[unit]
                alternatives = sorted((t for t in operational if t["team_id"] != preferred and fits(unit, t, {})), key=lambda t: (-sum(assigned.get(f) == t["team_id"] for pid in unit for f in friends[pid]), t["team_id"]))
                choices = [preferred, *[t["team_id"] for t in alternatives]]
                placed = False
                for allow_low_availability_tradeoff in (False, True):
                    for team_id in choices:
                        if team_id != preferred and not fits(unit, by_team[team_id], {}): continue
                        for pid in unit: rosters[team_id].append(pid); assigned[pid] = team_id; stage_of[pid] = "cluster"
                        if everyone_still_fits(allow_low_availability_tradeoff):
                            placed = True
                            break
                        for pid in unit: rosters[team_id].remove(pid); del assigned[pid]; del stage_of[pid]
                    if placed:
                        break
        for group in components:
            place_group(group, "general", True)
        cluster_rows = []
        for cluster_number, comp in enumerate(self.friend_components, 1):
            if len(comp) < 3: continue
            members = set(comp)
            requests = [(r["source_reg_number"], r["resolved_id"]) for r in self.friend_resolutions if r["status"] == "resolved" and r["source_reg_number"] in members]
            for pid in comp:
                player = self.player_by_id[pid]; team = by_team.get(assigned.get(pid, ""), {})
                mine = [target for source, target in requests if source == pid]
                met = sum(pid in assigned and assigned.get(t) == assigned[pid] for t in mine)
                cluster_rows.append({"cluster_id": f"cluster-{cluster_number:02d}", "cluster_size": str(len(comp)), "reg_number": pid, "player_name": player["player_name"],
                                     "availability": availability_pattern(player), "assigned_team_id": assigned.get(pid, ""),
                                     "team_slot": f"{team.get('day', '')} {team.get('time', '')}".strip(),
                                     "can_attend_team_slot": "yes" if team and slot_ok(player, team) else "",
                                     "requests_satisfied": str(met), "requests_unmet": str(len(mine) - met),
                                     "seed_team": "yes" if team and sum(1 + len(g["friends"]) for g in self.anchor_groups.get(team["team_id"], [])) < SEED_TARGET else "no" if team else ""})
        summary_rows = []
        for t in self.teams:
            tid = t["team_id"]; mine = [r for r in anchor_rows if r["team_id"] == tid]
            blocked_counts = collections_counter(r["outcome"] for r in mine if r["outcome"] != "on_team")
            summary_rows.append({"team_id": tid, "coach_count": str(len(self.team_coach_ids(t))), "daughters_linked": str(len(self.anchor_groups.get(tid, []))),
                                 "anchor_members_placed": str(sum(r["outcome"] == "on_team" for r in mine)), "final_roster_size": str(len(rosters[tid])),
                                 "cluster_members_placed": str(sum(1 for pid in rosters[tid] if stage_of.get(pid) == "cluster")),
                                 "spots_left_to_max": str(roster["maximum"] - len(rosters[tid])), "blocked": "; ".join(f"{k}={v}" for k, v in sorted(blocked_counts.items()))})
        write_csv(self.output_dir / "05_anchor_team_summary.csv", ["team_id", "coach_count", "daughters_linked", "anchor_members_placed", "final_roster_size", "cluster_members_placed", "spots_left_to_max", "blocked"], summary_rows)
        write_csv(self.output_dir / "07_friend_clusters.csv", ["cluster_id", "cluster_size", "reg_number", "player_name", "availability", "assigned_team_id", "team_slot", "can_attend_team_slot", "requests_satisfied", "requests_unmet", "seed_team"], cluster_rows)
        self.assignments, self.unassigned, self.rosters = assigned, [p["reg_number"] for p in self.players if p["reg_number"] not in assigned], rosters
        self.low_availability_tradeoffs = [
            self.player_by_id[player_id]
            for player_id in self.unassigned
            if player_id in self.low_availability_yield_ids
        ]
        for t in self.teams:
            if len(rosters[t["team_id"]]) < roster["minimum"]:
                issues.append({"reg_number": "", "severity": "error", "issue": "team below minimum roster", "detail": f"{t['team_id']} has {len(rosters[t['team_id']])}"})
        self.assignment_issues = issues
        roster_rows = [{"team_id": t["team_id"], "division": self.division, "reg_number": pid, "player_name": self.player_by_id[pid]["player_name"]} for t in self.teams for pid in rosters[t["team_id"]]]
        player_rows = [{"reg_number": p["reg_number"], "player_name": p["player_name"], "division": p["division"], "team_id": assigned.get(p["reg_number"], ""), "assignment_status": "assigned" if p["reg_number"] in assigned else "unassigned", "placement_stage": stage_of.get(p["reg_number"], "")} for p in self.players]
        outcomes = []
        for r in self.friend_resolutions:
            status = "unresolved" if r["status"] != "resolved" else "satisfied" if assigned.get(r["source_reg_number"]) == assigned.get(r["resolved_id"]) and r["source_reg_number"] in assigned else "not_satisfied"
            outcomes.append({"request_id": r["request_id"], "request_type": "friend", "source_reg_number": r["source_reg_number"], "requested_id": r["resolved_id"], "resolution_status": r["status"], "outcome": status})
        for r in self.coach_resolutions:
            team = next((t for t in self.teams if t["team_id"] == assigned.get(r["source_reg_number"])), {})
            status = "unresolved" if r["status"] not in {"resolved", "any_coach"} else "satisfied" if r["status"] == "any_coach" and r["source_reg_number"] in assigned else "satisfied" if (bool(team) and r["resolved_id"] in self.team_coach_ids(team)) else "not_satisfied"
            outcomes.append({"request_id": r["request_id"], "request_type": "coach", "source_reg_number": r["source_reg_number"], "requested_id": r["resolved_id"], "resolution_status": r["status"], "outcome": status})
        self.request_outcomes = outcomes
        roster_rows.sort(key=lambda r: (r["team_id"], self.player_by_id[r["reg_number"]]["last_name"].casefold(), self.player_by_id[r["reg_number"]]["first_name"].casefold(), r["reg_number"]))
        write_csv(self.output_dir / "07_team_rosters.csv", ["team_id", "division", "reg_number", "player_name"], roster_rows)
        write_csv(self.output_dir / "07_player_assignments.csv", ["reg_number", "player_name", "division", "team_id", "assignment_status", "placement_stage"], player_rows)
        write_csv(self.output_dir / "07_request_outcomes.csv", ["request_id", "request_type", "source_reg_number", "requested_id", "resolution_status", "outcome"], outcomes)
        write_csv(self.output_dir / "07_assignment_issues.csv", ["reg_number", "severity", "issue", "detail"], issues)

    def _report(self) -> None:
        quality = []; summary = []
        for team in self.teams:
            ids = self.rosters[team["team_id"]]
            friend_ok = sum(1 for r in self.request_outcomes if r["request_type"] == "friend" and r["outcome"] == "satisfied" and self.assignments.get(r["source_reg_number"]) == team["team_id"])
            quality.append({"team_id": team["team_id"], "roster_size": str(len(ids)), "friend_requests_satisfied": str(friend_ok), "available_schedule": "yes" if team["slot_id"] else "no"})
            summary.append({"team_id": team["team_id"], "division": self.division, "head_coach_id": team["head_coach_id"], "head_coach_name": self.coach_by_id.get(team["head_coach_id"], {}).get("coach_name", ""), "co_head_coach_id": team["co_head_coach_id"], "co_head_coach_name": self.coach_by_id.get(team["co_head_coach_id"], {}).get("coach_name", ""), "additional_coach_names": "; ".join(self.coach_by_id[x]["coach_name"] for x in team["extra_coach_ids"].split(";") if x in self.coach_by_id), "slot_id": team["slot_id"], "day": team["day"], "time": team["time"], "location": team["location"], "roster_size": str(len(ids))})
        resolution_by_id = {
            resolution["request_id"]: resolution
            for resolution in [*self.friend_resolutions, *self.coach_resolutions]
        }
        unmet = []
        for outcome in self.request_outcomes:
            if outcome["outcome"] == "satisfied":
                continue
            player = self.player_by_id[outcome["source_reg_number"]]
            resolution = resolution_by_id[outcome["request_id"]]
            source_slots = available_slots(player, self.slots)
            target_slots: list[dict[str, str]] = []
            shared_slots: list[dict[str, str]] = []
            if outcome["outcome"] == "unresolved":
                reason = "AMBIGUOUS_REQUEST" if outcome["resolution_status"] == "ambiguous" else "UNRESOLVED_REQUEST"
                detail = "Multiple registered matches require clarification." if reason == "AMBIGUOUS_REQUEST" else "No registered match was found for the requested person."
                action = "Contact household to clarify the requested person."
            elif outcome["request_type"] == "friend":
                target = self.player_by_id[resolution["resolved_id"]]
                target_slots = available_slots(target, self.slots)
                shared_slots = [slot for slot in source_slots if slot["slot_id"] in {candidate["slot_id"] for candidate in target_slots}]
                if shared_slots:
                    reason = "TEAM_ASSIGNMENT_TRADEOFF"
                    detail = "Shared practice slots existed, but roster capacity or higher-priority constraints placed the players on different teams."
                    action = "Review the roster tradeoff before contacting the household."
                else:
                    reason = "NO_SHARED_PRACTICE_SLOT"
                    detail = "The players have no practice slot in common."
                    action = "Contact household: the requested friendship is impossible with current availability."
            else:
                requested_coach_teams = [
                    candidate
                    for candidate in self.teams
                    if resolution["resolved_id"] in self.team_coach_ids(candidate) and candidate["slot_id"]
                ]
                reason = unmet_coach_request_reason(
                    player,
                    requested_coach_teams,
                    self.rosters,
                    int(self.config["roster"]["maximum"]),
                )
                if reason == "COACH_TEAM_SLOT_UNAVAILABLE":
                    detail = "The requested coach's team practices at a time the player cannot attend."
                    action = "Contact household: the requested coach team practice is incompatible with the player's availability."
                elif reason == "COACH_TEAM_FULL":
                    detail = "The requested coach's team has reached the maximum roster size."
                    action = "Contact household: the requested coach team was full."
                else:
                    detail = "The requested coach was not assigned to the player's final team."
                    action = "Contact household: the requested coach could not be assigned."
            unmet.append({
                **outcome,
                "player_name": player["player_name"],
                "submitter_name": player["submitter_name"],
                "submitter_email": player["submitter_email"],
                "requested_name": resolution["requested_name"],
                "assigned_team_id": self.assignments.get(player["reg_number"], ""),
                "unmet_reason": reason,
                "unmet_reason_detail": detail,
                "source_availability": availability_pattern(player),
                "requested_availability": availability_pattern(self.player_by_id[resolution["resolved_id"]]) if outcome["request_type"] == "friend" and resolution["status"] == "resolved" else "",
                "shared_slot_ids": ";".join(slot["slot_id"] for slot in shared_slots),
                "recommended_action": action,
            })
        priority = {"NO_SHARED_PRACTICE_SLOT": 0, "COACH_TEAM_SLOT_UNAVAILABLE": 0, "COACH_TEAM_FULL": 0, "COACH_ASSIGNED_TO_DIFFERENT_TEAM": 1, "TEAM_ASSIGNMENT_TRADEOFF": 2, "AMBIGUOUS_REQUEST": 3, "UNRESOLVED_REQUEST": 4}
        per_player: dict[str, list[dict[str, str]]] = defaultdict(list)
        for outcome in self.request_outcomes: per_player[outcome["source_reg_number"]].append(outcome)
        unmet_by_player: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in unmet: unmet_by_player[row["source_reg_number"]].append(row)
        no_request_met = []
        for pid, outcomes in per_player.items():
            if any(o["outcome"] == "satisfied" for o in outcomes): continue
            player = self.player_by_id[pid]
            rows = sorted(unmet_by_player[pid], key=lambda r: (priority.get(r["unmet_reason"], 9), r["request_id"]))
            primary = rows[0]
            no_request_met.append({
                "reg_number": pid, "player_name": player["player_name"], "submitter_name": player["submitter_name"], "submitter_email": player["submitter_email"],
                "assigned_team_id": self.assignments.get(pid, ""), "availability": availability_pattern(player),
                "request_count": str(len(outcomes)), "unresolved_count": str(sum(o["outcome"] == "unresolved" for o in outcomes)),
                "unmet_count": str(sum(o["outcome"] == "not_satisfied" for o in outcomes)),
                "requested_names": "; ".join(r["requested_name"] for r in rows), "primary_reason": primary["unmet_reason"],
                "recommended_action": primary["recommended_action"],
                "_sort": (priority.get(primary["unmet_reason"], 9) >= 3, priority.get(primary["unmet_reason"], 9), pid),
            })
        no_request_met.sort(key=lambda r: r["_sort"])
        write_csv(self.output_dir / "08_players_no_request_met.csv", ["reg_number", "player_name", "submitter_name", "submitter_email", "assigned_team_id", "availability", "request_count", "unresolved_count", "unmet_count", "requested_names", "primary_reason", "recommended_action"], no_request_met)
        no_friend_request_met = []
        for pid, outcomes in per_player.items():
            friend_outcomes = [outcome for outcome in outcomes if outcome["request_type"] == "friend"]
            if not friend_outcomes or any(outcome["outcome"] == "satisfied" for outcome in friend_outcomes):
                continue
            player = self.player_by_id[pid]
            rows = sorted(
                (row for row in unmet_by_player[pid] if row["request_type"] == "friend"),
                key=lambda row: (priority.get(row["unmet_reason"], 9), row["request_id"]),
            )
            primary = rows[0]
            no_friend_request_met.append({
                "reg_number": pid, "player_name": player["player_name"], "submitter_name": player["submitter_name"], "submitter_email": player["submitter_email"],
                "assigned_team_id": self.assignments.get(pid, ""), "availability": availability_pattern(player),
                "friend_request_count": str(len(friend_outcomes)),
                "friend_requests_unresolved": str(sum(outcome["outcome"] == "unresolved" for outcome in friend_outcomes)),
                "friend_requests_unmet": str(sum(outcome["outcome"] == "not_satisfied" for outcome in friend_outcomes)),
                "requested_names": "; ".join(row["requested_name"] for row in rows), "primary_reason": primary["unmet_reason"],
                "recommended_action": primary["recommended_action"],
                "_sort": (priority.get(primary["unmet_reason"], 9) >= 3, priority.get(primary["unmet_reason"], 9), pid),
            })
        no_friend_request_met.sort(key=lambda row: row["_sort"])
        write_csv(
            self.output_dir / "08_friend_requesters_no_friend_met.csv",
            ["reg_number", "player_name", "submitter_name", "submitter_email", "assigned_team_id", "availability", "friend_request_count", "friend_requests_unresolved", "friend_requests_unmet", "requested_names", "primary_reason", "recommended_action"],
            no_friend_request_met,
        )
        low_rows = []
        for pid, outcomes in per_player.items():
            player = self.player_by_id[pid]
            open_count = sum((player[f"{d}_early"] == "yes") + (player[f"{d}_late"] == "yes") for d in DAYS)
            if open_count >= LOW_AVAILABILITY_WARNING: continue
            met = sum(o["outcome"] == "satisfied" for o in outcomes)
            low_rows.append({"reg_number": pid, "player_name": player["player_name"], "submitter_name": player["submitter_name"], "submitter_email": player["submitter_email"],
                             "availability": availability_pattern(player), "open_slots_of_10": str(open_count), "request_count": str(len(outcomes)), "requests_satisfied": str(met),
                             "assigned_team_id": self.assignments.get(pid, ""),
                             "recommended_action": "Tell the family that limited availability may prevent their requests being met" if met < len(outcomes) else "No action: all requests met"})
        low_rows.sort(key=lambda r: (int(r["open_slots_of_10"]), r["reg_number"]))
        write_csv(self.output_dir / "08_low_availability_requesters.csv", ["reg_number", "player_name", "submitter_name", "submitter_email", "availability", "open_slots_of_10", "request_count", "requests_satisfied", "assigned_team_id", "recommended_action"], low_rows)
        tradeoff_rows = [
            {
                "reg_number": player["reg_number"],
                "player_name": player["player_name"],
                "submitter_name": player["submitter_name"],
                "submitter_email": player["submitter_email"],
                "availability": availability_pattern(player),
                "open_slots_of_10": str(self._open_slots(player)),
                "reason": "LOW_AVAILABILITY_PRIORITY_TRADEOFF",
                "detail": "Player has fewer than five open practice windows and no friend, coach, or anchor relationship; a request-priority schedule left no compatible team.",
                "recommended_action": "Contact household: broaden availability to improve assignment options.",
            }
            for player in self.low_availability_tradeoffs
        ]
        write_csv(
            self.output_dir / "08_low_availability_tradeoffs.csv",
            ["reg_number", "player_name", "submitter_name", "submitter_email", "availability", "open_slots_of_10", "reason", "detail", "recommended_action"],
            tradeoff_rows,
        )
        final = list(self.validation_issues) + self.coach_assignment_issues + self.assignment_issues
        for p in self.players:
            if p["reg_number"] in self.unassigned: final.append({"stage": "final", "severity": "error", "entity_type": "player", "entity_id": p["reg_number"], "issue": "unassigned player", "detail": p["player_name"]})
        if not any(r.get("severity") == "error" for r in final): final.append({"stage": "final", "severity": "pass", "entity_type": "pipeline", "entity_id": self.division, "issue": "all hard checks passed", "detail": ""})
        write_csv(self.output_dir / "08_roster_quality.csv", ["team_id", "roster_size", "friend_requests_satisfied", "available_schedule"], quality)
        write_csv(self.output_dir / "08_team_summary.csv", ["team_id", "division", "head_coach_id", "head_coach_name", "co_head_coach_id", "co_head_coach_name", "additional_coach_names", "slot_id", "day", "time", "location", "roster_size"], summary)
        write_csv(self.output_dir / "08_unmet_requests.csv", ["request_id", "request_type", "source_reg_number", "player_name", "submitter_name", "submitter_email", "requested_name", "requested_id", "resolution_status", "outcome", "assigned_team_id", "unmet_reason", "unmet_reason_detail", "source_availability", "requested_availability", "shared_slot_ids", "recommended_action"], unmet)
        write_csv(self.output_dir / "08_final_validation.csv", ["stage", "severity", "entity_type", "entity_id", "issue", "detail", "team_id", "reg_number"], final)
        write_csv(
            self.output_dir / "09_assigned_practice_slots.csv",
            ASSIGNED_PRACTICE_SLOT_FIELDS,
            assigned_practice_slot_rows(self.teams, self.coach_by_id, calendar_first=True),
        )


def solve_slots(pipelines: list["Pipeline"]) -> dict[str, dict[str, dict[str, tuple[Any, ...]]]]:
    """Assign one practice slot to every team of every division at once; a slot hosts at most one team in total."""
    items = [(pl, team) for pl in pipelines for team in pl.teams]
    for _, team in items: team.update({"slot_id": "", "day": "", "time": "", "location": ""})
    all_ids = {sid for pl in pipelines for sid in pl.slot_by_id}
    taken: dict[str, str] = {}
    chosen: dict[str, list[dict[str, str]]] = {pl.division: [] for pl in pipelines}
    pending: dict[str, list[dict[str, str]]] = {pl.division: list(pl.teams) for pl in pipelines}
    day_counts: dict[str, int] = defaultdict(int)
    # Most constrained teams (fewest candidate slots) choose first; forced overrides therefore win their slot.
    order = sorted(items, key=lambda it: (len(it[0].team_info[it[1]["team_id"]]["valid"]) or 10**6, [p.division for p in pipelines].index(it[0].division), it[1]["team_id"]))
    for pl, team in order:
        pending[pl.division].remove(team)
        free = all_ids - set(taken)
        scored = []
        for slot in pl.team_info[team["team_id"]]["valid"]:
            if slot["slot_id"] not in free: continue
            vec = pl.slot_vector(team, slot, chosen[pl.division], pending[pl.division], free)
            scored.append((vec, -day_counts[slot["day"]], slot))
        if not scored: continue
        _, _, slot = max(scored, key=lambda x: (x[0], x[1], [-ord(c) for c in x[2]["slot_id"]]))
        team.update({k: slot[k] for k in ("slot_id", "day", "time", "location")})
        taken[slot["slot_id"]] = team["team_id"]; chosen[pl.division].append(slot); day_counts[slot["day"]] += 1

    cache: dict[tuple[Any, ...], tuple[Any, ...]] = {}
    def team_vector(pl: "Pipeline", team: dict[str, str], slot: dict[str, str], assignment: dict[str, str]) -> tuple[Any, ...]:
        others = tuple(sorted(sid for tid, sid in assignment.items() if tid != team["team_id"] and sid))
        key = (team["team_id"], slot["slot_id"], others)
        if key not in cache:
            cache[key] = pl.slot_vector(team, slot, [pl.slot_by_id[x] for x in others], [], set())
        return cache[key]
    def division_total(pl: "Pipeline", assignment: dict[str, str]) -> tuple[Any, ...]:
        total: list[Any] = []
        for team in pl.teams:
            sid = assignment.get(team["team_id"])
            if not sid: continue
            vector = team_vector(pl, team, pl.slot_by_id[sid], assignment)
            if not total: total = [0] * len(vector)
            for i, v in enumerate(vector): total[i] += v
        selected_slots = [pl.slot_by_id[sid] for sid in assignment.values() if sid]
        total[6] = sum(
            any(all(slot_ok(pl.player_by_id[player_id], slot) for player_id in pair) for slot in selected_slots)
            for pair in pl.mutual_friend_pairs
        )
        return tuple(total)
    def current(pl: "Pipeline") -> dict[str, str]:
        return {t["team_id"]: t["slot_id"] for t in pl.teams}
    def spread_cost() -> int:
        counts: dict[str, int] = defaultdict(int)
        for _, t in items:
            if t["slot_id"]: counts[(t["day"], t["time"])] += 1
        return sum(c * c for c in counts.values())
    by_division = {pl.division: pl for pl in pipelines}
    def set_slot(team: dict[str, str], slot: dict[str, str] | None) -> None:
        if slot is None: team.update({"slot_id": "", "day": "", "time": "", "location": ""})
        else: team.update({k: slot[k] for k in ("slot_id", "day", "time", "location")})
    def try_change(changes: list[tuple["Pipeline", dict[str, str], dict[str, str]]], require_spread: bool) -> bool:
        affected = {pl.division for pl, _, _ in changes}
        before_totals = {d: division_total(by_division[d], current(by_division[d])) for d in affected}
        before_spread = spread_cost()
        old = [(team, {k: team[k] for k in ("slot_id", "day", "time", "location")}) for _, team, _ in changes]
        for _, team, slot in changes: set_slot(team, slot)
        after_totals = {d: division_total(by_division[d], current(by_division[d])) for d in affected}
        before = tuple(sum(col) for col in zip(*before_totals.values()))
        after = tuple(sum(col) for col in zip(*after_totals.values()))
        better = after > before if not require_spread else (after == before and spread_cost() < before_spread)
        if better: return True
        for team, saved in old: team.update(saved)
        return False
    for require_spread in (False, True):
        for _ in range(8):
            changed = False
            used = {t["slot_id"]: (pl, t) for pl, t in items if t["slot_id"]}
            for pl, team in items:
                if not team["slot_id"] and not require_spread: continue
                for slot in pl.team_info[team["team_id"]]["valid"]:
                    if slot["slot_id"] == team["slot_id"] or not team["slot_id"]: continue
                    if slot["slot_id"] not in used:
                        if try_change([(pl, team, slot)], require_spread):
                            used = {t["slot_id"]: (p2, t) for p2, t in items if t["slot_id"]}; changed = True
                    else:
                        pl2, other = used[slot["slot_id"]]
                        mine = pl.slot_by_id[team["slot_id"]]
                        if other["team_id"] == team["team_id"] or mine not in pl2.team_info[other["team_id"]]["valid"]: continue
                        if try_change([(pl, team, slot), (pl2, other, mine)], require_spread):
                            used = {t["slot_id"]: (p2, t) for p2, t in items if t["slot_id"]}; changed = True
            if not changed: break
    result: dict[str, dict[str, dict[str, tuple[Any, ...]]]] = {}
    for pl in pipelines:
        assignment = current(pl)
        result[pl.division] = {t["team_id"]: {s["slot_id"]: team_vector(pl, t, s, assignment) for s in pl.team_info[t["team_id"]]["valid"]} for t in pl.teams}
    return result


def run_divisions(pipelines: list["Pipeline"]) -> dict[str, dict[str, int]]:
    for pl in pipelines: pl.prepare()
    vectors = solve_slots(pipelines)
    return {pl.division: pl.finish(vectors[pl.division]) for pl in pipelines}


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh: return json.load(fh)
