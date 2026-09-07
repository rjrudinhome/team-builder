# Volleyball team builder

A deterministic, standard-library-only Python 3.11 pipeline for forming Spring 2026 volleyball teams. It preserves every decision as CSV; it does **not** silently place a player or coach where availability, division, or roster-cap constraints forbid it.

## Run

```bash
python3 -m team_builder --division 4th
```

From `team-builder/`, the command reads `../otto-data/updated-players.csv`, `coaches.csv`, and `practice-slots.csv`; uses `../otto-data/2026-4th-overrides.csv` when it exists; and recreates reports in `../otto-data/runs/4th/`. `--division` also accepts a comma-separated list (`4th,5th,6th`) or `all`. Practice slots come from one shared pool, so with several divisions every team of every selected division gets its slot in a single joint step: coach availability is always required, the most constrained teams choose first, then moves and swaps improve the coach-cluster fit across divisions, and an even spread over the week is only a last tiebreak. A division runs only when its `2026-<division>-overrides.csv` exists (the confirmed head coaches come from it); others are skipped and named in the console and `runs/skipped_divisions.csv`. A multi-division run also writes `runs/all_team_slots.csv` (one row per slot in use). `--output-dir` and `--overrides` apply to a single division only. Run all divisions together once their overrides exist so no slot is double-booked; separate single-division runs do not know about each other's slots. Every default path is derived from `--division`. Use `--input-dir`, `--players-file ../otto-data/players.csv`, `--overrides`, `--output-dir`, or `--config config/2026.json` to override a default.

Each concise grade completion line reports the unassigned count, `friend requesters with none met`, and assigned-practice counts by weekday (`M`, `T`, `W`, `R`, `F`). The friend metric includes unresolved/ambiguous friend requests and intentionally ignores coach-request outcomes, so a satisfied coach request does not hide a friend-placement follow-up. Multi-grade runs also print the total across completed grades. `08_friend_requesters_no_friend_met.csv` lists exactly the players counted by that metric. Use `08_unmet_requests.csv` to identify every individual request and reason; `08_players_no_request_met.csv` remains the broader combined friend-and-coach report.

### Select-league forecast

Before Select tryouts, run an isolated scenario with a private CSV containing projected Select **registration numbers** and a JSON object of scenario-only team counts. Neither input changes `updated-players.csv`, the main configuration, or production reports:

```bash
python3 -m team_builder \
  --division 6th,7th,8th \
  --forecast-name select-projection \
  --forecast-select ../otto-data/projected-select.csv \
  --forecast-team-counts ../otto-data/select-forecast-team-counts.json
```

Use [`docs/projected-select-template.csv`](docs/projected-select-template.csv) and [`docs/forecast-team-counts-template.json`](docs/forecast-team-counts-template.json) as templates, but store completed files outside this repository. Forecast reports are written only to `../otto-data/runs/forecasts/select-projection/`; `00_projected_select_exclusions.csv` audits every player removed from each selected division. A count-only estimate can size teams and recruit coaches, but a projected player list is required to evaluate friend requests, coach children, and roster composition. After tryouts, replace the projected IDs with the confirmed Select roster and rerun the scenario to generate final recreation teams.

## Overrides

Copy [`docs/overrides-template.csv`](docs/overrides-template.csv). Valid constraint types are:

* `force_friend`: `subject_id` and `target_id` are player **Reg Number** values. This is a hard co-roster requirement.
* `separate_players`: the two Reg Numbers cannot share a roster.
* `assign_head_coach`: `subject_id` is a team id (for example `4th-01`) and `target_id` is a coach **UUID**. The coach must exist in that division.
* `assign_co_head_coach`: adds a coach to the specified team; `target_id` is a coach **UUID** in that division. A team may have any number of these rows. The first (config rows first) is a required co-head whose availability constrains the practice slot; additional coaches are assistants who are assigned regardless (unless they have a linked registered daughter, in which case the slot must also suit them so she can be placed), with a warning in `05_coach_assignment_issues.csv` if they cannot attend the slot. A coach cannot be assigned to two teams.
* `assign_practice_slot`: `subject_id` is a team id and `target_id` is either a `slot_id` from normalized slots or the practice written as `Day Time Location` (for example `Monday Early Longfellow Aux`, optionally `|`-separated). Give one row per team to reuse last season's practices instead of generating them. The slot must exist and all assigned coaches must be available.
* `link_coach_daughter`: `subject_id` is a coach **UUID** and `target_id` is a player **Reg Number**. Use it when the automatic email link below misses a coach's daughter.

Invalid, conflicting, or infeasible override rows are retained in `03_constraints.csv` and reported; they are never guessed or applied unsafely. A division with no overrides file is not run (see above).

Store overrides with actual UUIDs and player registration numbers in `../otto-data/`, not this repository:

```bash
python3 -m team_builder --division 4th
```

When `--output-dir` is inside any Git repository, the requested path must already be Git-ignored. This prevents accidentally writing roster exports to a stageable path; this repository's `runs/` directory is preconfigured for that purpose.

## Decision policy

Names are normalized conservatively. Only exact normalized names resolve automatically. A coach request must name one coach; an abbreviated, multi-name, zero-match, or multi-match request remains actionable (`invalid`, `unresolved`, or `ambiguous`) for source-data cleanup and is never guessed or split. Confirmed head coaches and co-coach pairings belong in a local ID-based override file, not the public configuration. The greedy assignment is repeatable: sorted identifiers break every tie.

All prominent stage CSVs are generated, including normalized inputs, candidate/review files, constraints, feasibility reports, coach and player assignments, quality reports, and final validation. `08_final_validation.csv` is the authoritative release check.

## Coach anchor teams

A coach (head, co-head or assistant) with `RegChild = Yes` is linked to the player named in the coach's `ChildName` column (exact normalized full name, one match in the division; a trailing "(nickname)" is ignored). When `ChildName` is blank the player whose **Submitter Email** equals the coach's volunteer email is used instead; `link_coach_daughter` overrides always apply. `05_coach_child_links.csv` lists each coach's link method and any `no_match`/`ambiguous` names to fix. Each team is anchored first: the daughter and every resolved inbound or outbound friend request form an early cluster when the complete group is at most 12 players and has no explicit separation conflict. Practice-slot selection chooses the largest coach-compatible subset that includes every linked child; that subset is placed atomically on the coach's team. Larger or conflicting groups remain diagnostic, and friends unavailable for the selected slot remain separately reported rather than bypassing roster or availability constraints. `05_anchor_teams.csv` lists each member's outcome (`on_team`, `no_shared_slot_with_coach`, `team_full`, `separated`, `already_placed_elsewhere`). It contains names and must stay private.

Practice-slot choice treats each team as a cluster of coaches, their daughters and friends, and players who requested those coaches. All coaches stay hard constraints, and the slot must suit every linked daughter when one exists. Mutually requesting friend pairs are prioritized ahead of larger, best-effort friend webs and division-wide availability coverage, so a compatible team slot exists for their protected placement pass. Coach/friend request clusters then outrank remaining coverage. A player with fewer than 5 of 10 slots open remains protected when they made or received a resolved friend/coach request or are in a coach-child anchor group. A low-availability player without any such relationship may yield, including becoming unassigned, rather than displacing an otherwise feasible coach or friend request. `08_low_availability_tradeoffs.csv` records those intentional outcomes; `08_low_availability_requesters.csv` lists players with fewer than 3 of 10 slots open who made requests, so families can be warned that limited availability may prevent their requests being met.

Anchor groups may fill a team to the roster maximum (12); remaining capacity is then rebalanced so other teams stay as even as possible. An anchor group is trimmed (non-mutual friends last, reported as `team_full`) only if the other teams could not otherwise reach the roster minimum. `05_anchor_team_summary.csv` shows each team's coaches, linked daughters, anchor members, final size, and spots left to the maximum.

Teams with a small anchor group (fewer than 8 members) are treated as seed teams for large friend webs: their practice slot is chosen to fit the largest web, and big webs prefer such teams when placed. If no single slot fits a whole web, it is split along slot lines; `07_friend_clusters.csv` shows `seed_team` and each member's attendance.

After anchors, players who requested a resolved head/co-head coach (and share that coach's slot) are placed before everyone else, so flexible-availability players are not crowded out of the coach's team. Unmet coach requests remain only when the player cannot attend the coach's slot, the team is full, or the requested person is not a head/co-head coach.

After anchors and coach requests, mutually resolved friend pairs are placed atomically on a shared feasible team before the general friend-cluster pass. This protects a mutual pair whenever availability, roster capacity, explicit separation constraints, and remaining-player feasibility permit it; one-way requests remain best-effort. Then a friend-cluster pass takes each connected web of resolved friend requests and places it as a whole: it first maximizes the number of requesters with at least one resolved outgoing friend request met, then the total number of satisfied friend links. This prevents an inbound one-way request from displacing a player's only satisfiable request. It seeds the team whose practice slot most members can attend, splits the rest along slot lines to the best second team, lets bridge players (available for more than one sub-group's slot) go where they satisfy the most requests, and finishes with a deterministic swap/move improvement. Coach-request matches outrank friend matches in this step. A placement is skipped if it would leave any remaining player with no slot-compatible team with room. Coach practice-slot choice also favors slots that fit large friend webs (coach availability and slot overrides stay hard). `07_player_assignments.csv` has a `placement_stage` column (`anchor`, `coach_request`, `mutual_pair`, `cluster`, `general`), and `07_friend_clusters.csv` shows each web of 3+ players with availability, assigned team slot, and satisfied/unmet requests, which makes availability-driven splits easy to spot.

## Registration follow-up

`04_friend_request_analysis.csv` is the registration-period follow-up queue: one row per friend request with the player's name, submitter name and email, requested text, matching result, and recommended action. `04_unresolved_friend_requests.csv` is the focused data-cleanup queue for friend requests with no registered-player match; update or clarify those names in private `updated-players.csv` during registration. Before assignments, `04_impossible_friend_clusters.csv` identifies every resolved friend cluster with no single practice slot that fits all members; it provides one contact-ready row per cluster member so staff can resolve the group during registration. `08_unmet_requests.csv` provides the equivalent contact-ready view after roster assignment for unresolved or unsatisfied friend and coach requests. It includes an `unmet_reason`; `NO_SHARED_PRACTICE_SLOT` means the resolved friend pair has no weekly Day/Time/Location in common, `COACH_TEAM_SLOT_UNAVAILABLE` means the player cannot attend the requested coach's assigned team practice, and `COACH_TEAM_FULL` means the player can attend but the requested coach team has reached the maximum roster size. Diagnostic availability uses five Monday-Friday characters: `E` early, `L` late, `O` either, and `X` unavailable. These files contain private registration data and must remain in a Git-ignored or external output directory.

## Assigned practice-slot summaries

Every grade run writes `09_assigned_practice_slots.csv`. A multi-grade run also writes `runs/all_assigned_practice_slots.csv`. Both files contain the compact scheduling view:

```text
Location,Day,Time,Team,Head Coach
```

Rows include assigned teams only. Per-grade summaries sort by Monday-Friday, Early-Late, location, then team; the combined all-grade report sorts by location, Monday-Friday, Early-Late, then team. `Location` is the practice school/site. The reports show the head coach only; use `05_coach_assignments.csv` or `08_team_summary.csv` when co-head and assistant assignments are also needed.

## One-time RosterBuddy availability update

From `team-builder/`, run:

```bash
python3 merge_rosterbuddy_availability.py
```

The command matches Otto `Reg Number` to RosterBuddy `RecordID` exactly, replaces availability only for matching records using the five `E` (early), `L` (late), `O` (either), and `X` (neither) codes, and writes `../otto-data/updated-players.csv`. It preserves every other Otto column, including friend and coach requests. `../otto-data/updated-players-merge-report.csv` records updates, unchanged Otto-only rows, and RosterBuddy-only likely withdrawals.

## Repeatable parent availability updates

The parent availability form must export `UUID`, `Registration Date/Time`, and `Monday` through `Friday`. `UUID` must be the hidden/prefilled player identifier; the importer ignores names and emails for matching. `Registration Date/Time` uses the form's `MM/DD/YYYY HH:MM:SS` format and determines which repeated response wins.

From `team-builder/`, test with the synthetic export:

```bash
python3 merge_availability_updates.py \
  --updates-file ../otto-data/availability-updates-test.csv \
  --output-file ../otto-data/updated-players-availability.csv \
  --report-file ../otto-data/updated-players-availability-report.csv
```

For regular use, replace `--updates-file` with the current form export or omit it to use `../otto-data/availability-updates.csv`. The command always reads `../otto-data/updated-players.csv` by default and writes a separate `updated-players-availability.csv`; it refuses to overwrite the canonical input. It applies only Monday-Friday values, preserving friend requests, coach requests, and every other player field.

The audit CSV records `UPDATED`, `SUPERSEDED`, `INVALID_UPDATE`, `UNKNOWN_UUID`, and `TIMESTAMP_TIE` outcomes. An invalid newer response does not replace a prior valid response. If multiple valid responses for the same UUID share the latest timestamp, all tied updates are rejected and the player's existing availability is retained.

`08_friend_requesters_no_friend_met.csv` lists exactly the terminal metric's friend-requesting players who have no satisfied friend request, including players whose coach request is satisfied. `08_players_no_request_met.csv` separately lists players who made at least one friend or coach request but had none of either type satisfied. Both include contact details, availability, and the most actionable unmet reason; players with no requests are excluded. These reports are private.

## Tests

```bash
python -m unittest discover -s tests -v
```
