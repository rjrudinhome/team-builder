import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from team_builder.availability_updates import merge_latest_availability
from team_builder.pipeline import (
    ASSIGNED_PRACTICE_SLOT_FIELDS,
    Pipeline,
    assigned_practice_slot_rows,
    availability,
    availability_pattern,
    available_slots,
    candidate_matches,
    players_with_no_friend_requests_met_count,
    run_divisions,
    unmet_coach_request_reason,
)
from team_builder.rosterbuddy_merge import merge_availability, parse_rosterbuddy_availability

ROOT = Path(__file__).parent
FIXTURES = ROOT / "fixtures"
OUTPUT = ROOT / "test-output"
CONFIG = {
    "team_counts": {"4th": 1},
    "age_group_labels": {"Age Group >4th Grade": "4th"},
    "roster": {"minimum": 1, "target": 2, "maximum": 3},
    "fixed_team_heads": {"4th": {"4th-01": "c-1"}},
    "fixed_co_heads": {},
}

class PipelineTests(unittest.TestCase):
    def tearDown(self):
        shutil.rmtree(OUTPUT, ignore_errors=True)

    def test_availability_parsing(self):
        self.assertEqual(availability("Available either slot"), (True, True, "either"))
        self.assertEqual(availability("Available early only - 5:30 pm to 7:30 pm"), (True, False, "early"))
        self.assertEqual(availability("Unavailable"), (False, False, "unavailable"))

    def test_matching_does_not_choose_ambiguous_name(self):
        records = [{"id": "1", "name": "Anna Bell"}, {"id": "2", "name": "Anna Brown"}]
        matches = candidate_matches("Anna", records, "name")
        self.assertEqual(matches, [])
        self.assertEqual(candidate_matches("Anna Bell", records, "name")[0][0]["id"], "1")

    def test_friend_availability_without_a_shared_slot(self):
        slots = [
            {"slot_id": "early", "day": "Monday", "time": "Early", "location": "Gym"},
            {"slot_id": "late", "day": "Monday", "time": "Late", "location": "Gym"},
        ]
        early_only = {"Monday_early": "yes", "Monday_late": "no"}
        late_only = {"Monday_early": "no", "Monday_late": "yes"}
        self.assertEqual(
            {slot["slot_id"] for slot in available_slots(early_only, slots)}
            & {slot["slot_id"] for slot in available_slots(late_only, slots)},
            set(),
        )
        self.assertEqual(
            availability_pattern({"Monday_early": "yes", "Monday_late": "no", "Tuesday_early": "no", "Tuesday_late": "yes"}),
            "ELXXX",
        )

    def test_impossible_friend_cluster_is_reported_before_assignment(self):
        data = Path(tempfile.mkdtemp())
        out = data / "out"
        early = "Available early only - 5:30 pm to 7:30 pm"
        none = "Unavailable"
        (data / "practice-slots.csv").write_text(
            "Day,Time,Location\nMonday,Early,G\nTuesday,Early,G\n",
            encoding="utf-8",
        )
        (data / "coaches.csv").write_text(
            "Volunteer First Name,Volunteer Last Name,Group Name,Volunteer Role,UUID,RegChild,Requests,Monday_1,Tuesday_1,Wednesday_1,Thursday_1,Friday_1\n"
            f"Al,Ace,Age Group >4th Grade,Head Coach,c-a,,,{early},{none},{none},{none},{none}\n"
            f"Bo,Boss,Age Group >4th Grade,Head Coach,c-b,,,{none},{early},{none},{none},{none}\n",
            encoding="utf-8",
        )
        header = "Player First Name,Player Last Name,Reg Number,Group Name,UUID,FriendRequests,CoachRequest,Monday,Tuesday,Wednesday,Thursday,Friday\n"
        players = [
            ("A", "Monday", "1", "B Bridge", early, none),
            ("B", "Bridge", "2", "A Monday; C Tuesday", early, early),
            ("C", "Tuesday", "3", "B Bridge", none, early),
            ("D", "Feasible", "4", "E Feasible", early, none),
            ("E", "Feasible", "5", "D Feasible", early, none),
        ]
        (data / "players.csv").write_text(header + "".join(
            f"{first},{last},{number},Age Group >4th Grade,u{number},{friends},,{monday},{tuesday},{none},{none},{none}\n"
            for first, last, number, friends, monday, tuesday in players
        ), encoding="utf-8")
        config = {
            **CONFIG,
            "team_counts": {"4th": 2},
            "roster": {"minimum": 1, "target": 3, "maximum": 3},
            "fixed_team_heads": {"4th": {"4th-01": "c-a", "4th-02": "c-b"}},
        }
        try:
            Pipeline("4th", data, out, config).run()
            with (out / "04_impossible_friend_clusters.csv").open(newline="") as fh:
                report = list(csv.DictReader(fh))
            self.assertEqual({row["reg_number"] for row in report}, {"1", "2", "3"})
            self.assertEqual({row["cluster_id"] for row in report}, {"cluster-01"})
            self.assertTrue(all(row["cluster_size"] == "3" for row in report))
            self.assertTrue(all(row["shared_slot_ids"] == "" for row in report))
            self.assertTrue(all(row["reason"] == "NO_COMMON_PRACTICE_SLOT_FOR_CLUSTER" for row in report))
            self.assertTrue(all("D Feasible" not in row["cluster_members"] for row in report))
        finally:
            shutil.rmtree(data, ignore_errors=True)

    def test_rosterbuddy_availability_codes(self):
        self.assertEqual(
            parse_rosterbuddy_availability("E, L, O, X, E"),
            {
                "Monday": "Available early only - 5:30 pm to 7:30 pm",
                "Tuesday": "Available late only - 7:00 pm to 9:00 pm",
                "Wednesday": "Available either slot",
                "Thursday": "Unavailable",
                "Friday": "Available early only - 5:30 pm to 7:30 pm",
            },
        )
        with self.assertRaises(ValueError):
            parse_rosterbuddy_availability("E, L")

    def test_rosterbuddy_merge_updates_only_exact_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            players = directory / "players.csv"
            rosterbuddy = directory / "rosterbuddy.csv"
            output = directory / "updated-players.csv"
            report = directory / "merge-report.csv"
            players.write_text(
                "Reg Number,Player First Name,Player Last Name,Monday,Tuesday,Wednesday,Thursday,Friday,FriendRequests\n"
                "1,Player,One,Unavailable,Unavailable,Unavailable,Unavailable,Unavailable,Keep this request\n"
                "2,Player,Two,Available either slot,Available either slot,Available either slot,Available either slot,Available either slot,Keep unchanged\n",
                encoding="utf-8",
            )
            rosterbuddy.write_text(
                "RecordID,Availability,FirstName,LastName\n"
                "1,\"E, L, O, X, E\",Player,One\n"
                "3,\"X, X, X, X, X\",Player,Three\n",
                encoding="utf-8",
            )
            summary = merge_availability(players, rosterbuddy, output, report)
            with output.open(newline="") as handle:
                updated = list(csv.DictReader(handle))
            self.assertEqual(updated[0]["Monday"], "Available early only - 5:30 pm to 7:30 pm")
            self.assertEqual(updated[0]["Tuesday"], "Available late only - 7:00 pm to 9:00 pm")
            self.assertEqual(updated[0]["FriendRequests"], "Keep this request")
            self.assertEqual(updated[1]["Monday"], "Available either slot")
            self.assertEqual(summary, {"UPDATED": 1, "OTTO_ONLY": 1, "ROSTERBUDDY_ONLY": 1})

    def test_rosterbuddy_merge_preserves_otto_rows_for_empty_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            players = directory / "players.csv"
            rosterbuddy = directory / "rosterbuddy.csv"
            output = directory / "updated-players.csv"
            report = directory / "merge-report.csv"
            players.write_text(
                "Reg Number,Monday,Tuesday,Wednesday,Thursday,Friday\n"
                "1,Available either slot,Unavailable,Unavailable,Unavailable,Unavailable\n",
                encoding="utf-8",
            )
            rosterbuddy.write_text("RecordID,Availability\n", encoding="utf-8")
            summary = merge_availability(players, rosterbuddy, output, report)
            with output.open(newline="") as handle:
                updated = list(csv.DictReader(handle))
            self.assertEqual(updated[0]["Monday"], "Available either slot")
            self.assertEqual(summary, {"OTTO_ONLY": 1})

    def test_availability_updates_use_latest_valid_uuid_response_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            players = directory / "updated-players.csv"
            updates = directory / "availability-updates.csv"
            output = directory / "updated-players-availability.csv"
            report = directory / "availability-report.csv"
            players.write_text(
                "UUID,Player First Name,Player Last Name,FriendRequests,CoachRequest,Monday,Tuesday,Wednesday,Thursday,Friday\n"
                "u-1,Player,One,Keep friend,Keep coach,Unavailable,Unavailable,Unavailable,Unavailable,Unavailable\n"
                "u-2,Player,Two,Keep friend 2,,Unavailable,Unavailable,Unavailable,Unavailable,Unavailable\n"
                "u-3,Player,Three,,,Unavailable,Unavailable,Unavailable,Unavailable,Unavailable\n"
                "u-4,Player,Four,,,Unavailable,Unavailable,Unavailable,Unavailable,Unavailable\n",
                encoding="utf-8",
            )
            updates.write_text(
                "UUID,Registration Date/Time,Monday,Tuesday,Wednesday,Thursday,Friday\n"
                "u-1,11/01/2026 09:00:00,Available early only - 5:30 pm to 7:30 pm,Unavailable,Unavailable,Unavailable,Unavailable\n"
                "u-1,11/01/2026 10:00:00,Available late only - 7:00 pm to 9:00 pm,Available either slot,Unavailable,Unavailable,Unavailable\n"
                "u-2,11/01/2026 09:00:00,Available either slot,Available either slot,Available either slot,Available either slot,Available either slot\n"
                "unknown,11/01/2026 09:00:00,Available either slot,Available either slot,Available either slot,Available either slot,Available either slot\n"
                "u-3,11/01/2026 09:00:00,Invalid value,Unavailable,Unavailable,Unavailable,Unavailable\n"
                "u-4,11/01/2026 09:00:00,Available either slot,Unavailable,Unavailable,Unavailable,Unavailable\n"
                "u-4,11/01/2026 09:00:00,Unavailable,Available either slot,Unavailable,Unavailable,Unavailable\n",
                encoding="utf-8",
            )

            summary = merge_latest_availability(players, updates, output, report)

            with output.open(newline="") as handle:
                updated = {row["UUID"]: row for row in csv.DictReader(handle)}
            with report.open(newline="") as handle:
                audit = list(csv.DictReader(handle))
            self.assertEqual(updated["u-1"]["Monday"], "Available late only - 7:00 pm to 9:00 pm")
            self.assertEqual(updated["u-1"]["Tuesday"], "Available either slot")
            self.assertEqual(updated["u-1"]["FriendRequests"], "Keep friend")
            self.assertEqual(updated["u-1"]["CoachRequest"], "Keep coach")
            self.assertEqual(updated["u-2"]["Wednesday"], "Available either slot")
            self.assertEqual(updated["u-3"]["Monday"], "Unavailable")
            self.assertEqual(updated["u-4"]["Monday"], "Unavailable")
            self.assertEqual(
                summary,
                {
                    "UPDATED": 2,
                    "SUPERSEDED": 1,
                    "UNKNOWN_UUID": 1,
                    "INVALID_UPDATE": 1,
                    "TIMESTAMP_TIE": 2,
                },
            )
            self.assertEqual(
                [row["status"] for row in audit],
                ["SUPERSEDED", "UPDATED", "UPDATED", "UNKNOWN_UUID", "INVALID_UPDATE", "TIMESTAMP_TIE", "TIMESTAMP_TIE"],
            )

    def test_availability_updates_reject_canonical_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            players = directory / "updated-players.csv"
            updates = directory / "availability-updates.csv"
            players.write_text(
                "UUID,Monday,Tuesday,Wednesday,Thursday,Friday\n"
                "u-1,Unavailable,Unavailable,Unavailable,Unavailable,Unavailable\n",
                encoding="utf-8",
            )
            updates.write_text(
                "UUID,Registration Date/Time,Monday,Tuesday,Wednesday,Thursday,Friday\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "must differ"):
                merge_latest_availability(players, updates, players, directory / "report.csv")

    def test_assigned_practice_slot_rows_support_location_and_calendar_sorts(self):
        coaches = {
            "c-1": {"coach_name": "Alex Alpha"},
            "c-2": {"coach_name": "Blair Beta"},
            "c-3": {"coach_name": "Casey Coach"},
            "c-4": {"coach_name": "Devon Delta"},
            "c-5": {"coach_name": "Evan Echo"},
        }
        teams = [
            {"team_id": "4th-04", "head_coach_id": "c-4", "slot_id": "4", "location": "Alpha School", "day": "Friday", "time": "Early"},
            {"team_id": "4th-02", "head_coach_id": "c-2", "slot_id": "2", "location": "Alpha School", "day": "Monday", "time": "Late"},
            {"team_id": "5th-01", "head_coach_id": "c-5", "slot_id": "5", "location": "Beta School", "day": "Monday", "time": "Early"},
            {"team_id": "4th-01", "head_coach_id": "c-1", "slot_id": "1", "location": "Alpha School", "day": "Monday", "time": "Early"},
            {"team_id": "4th-03", "head_coach_id": "c-3", "slot_id": "3", "location": "Alpha School", "day": "Thursday", "time": "Early"},
            {"team_id": "4th-unassigned", "head_coach_id": "c-1", "slot_id": "", "location": "", "day": "", "time": ""},
        ]

        rows = assigned_practice_slot_rows(teams, coaches)
        calendar_rows = assigned_practice_slot_rows(teams, coaches, calendar_first=True)

        self.assertEqual(list(rows[0]), ASSIGNED_PRACTICE_SLOT_FIELDS)
        self.assertEqual(
            [(row["Location"], row["Day"], row["Time"], row["Team"], row["Head Coach"]) for row in rows],
            [
                ("Alpha School", "Monday", "Early", "4th-01", "Alex Alpha"),
                ("Alpha School", "Monday", "Late", "4th-02", "Blair Beta"),
                ("Alpha School", "Thursday", "Early", "4th-03", "Casey Coach"),
                ("Alpha School", "Friday", "Early", "4th-04", "Devon Delta"),
                ("Beta School", "Monday", "Early", "5th-01", "Evan Echo"),
            ],
        )
        self.assertEqual(
            [(row["Location"], row["Day"], row["Time"], row["Team"]) for row in calendar_rows],
            [
                ("Alpha School", "Monday", "Early", "4th-01"),
                ("Beta School", "Monday", "Early", "5th-01"),
                ("Alpha School", "Monday", "Late", "4th-02"),
                ("Alpha School", "Thursday", "Early", "4th-03"),
                ("Alpha School", "Friday", "Early", "4th-04"),
            ],
        )

    def test_friend_requesters_with_no_friend_requests_met_excludes_coach_outcomes(self):
        outcomes = [
            {"request_type": "friend", "source_reg_number": "1", "outcome": "not_satisfied"},
            {"request_type": "coach", "source_reg_number": "1", "outcome": "satisfied"},
            {"request_type": "friend", "source_reg_number": "2", "outcome": "satisfied"},
            {"request_type": "friend", "source_reg_number": "2", "outcome": "not_satisfied"},
            {"request_type": "coach", "source_reg_number": "3", "outcome": "not_satisfied"},
            {"request_type": "friend", "source_reg_number": "4", "outcome": "unresolved"},
        ]

        self.assertEqual(players_with_no_friend_requests_met_count(outcomes), 2)

    def test_friend_only_followup_report_includes_satisfied_coach_requester(self):
        data = Path(tempfile.mkdtemp())
        out = data / "out"
        early = "Available early only - 5:30 pm to 7:30 pm"
        none = "Unavailable"
        (data / "practice-slots.csv").write_text(
            "Day,Time,Location\nMonday,Early,Gym A\nTuesday,Early,Gym B\n",
            encoding="utf-8",
        )
        (data / "coaches.csv").write_text(
            "Volunteer First Name,Volunteer Last Name,Group Name,Volunteer Role,UUID,RegChild,Requests,Monday_1,Tuesday_1,Wednesday_1,Thursday_1,Friday_1\n"
            f"Cora,Coach,Age Group >4th Grade,Head Coach,c-a,,,{early},{none},{none},{none},{none}\n"
            f"Bo,Boss,Age Group >4th Grade,Head Coach,c-b,,,{none},{early},{none},{none},{none}\n",
            encoding="utf-8",
        )
        header = "Player First Name,Player Last Name,Reg Number,Group Name,UUID,FriendRequests,CoachRequest,Monday,Tuesday,Wednesday,Thursday,Friday\n"
        rows = [
            ("Friend", "Requester", "1", "Tuesday Friend", "Cora Coach", early, none),
            ("Tuesday", "Friend", "2", "", "", none, early),
        ]
        (data / "players.csv").write_text(
            header
            + "".join(
                f"{first},{last},{number},Age Group >4th Grade,u{number},{friends},{coach},{monday},{tuesday},{none},{none},{none}\n"
                for first, last, number, friends, coach, monday, tuesday in rows
            ),
            encoding="utf-8",
        )
        config = {
            **CONFIG,
            "team_counts": {"4th": 2},
            "roster": {"minimum": 1, "target": 1, "maximum": 1},
            "fixed_team_heads": {"4th": {"4th-01": "c-a", "4th-02": "c-b"}},
        }
        try:
            Pipeline("4th", data, out, config).run()
            with (out / "08_friend_requesters_no_friend_met.csv").open(newline="") as fh:
                friend_followups = list(csv.DictReader(fh))
            with (out / "08_players_no_request_met.csv").open(newline="") as fh:
                combined_followups = list(csv.DictReader(fh))
            self.assertEqual([row["reg_number"] for row in friend_followups], ["1"])
            self.assertEqual(friend_followups[0]["primary_reason"], "NO_SHARED_PRACTICE_SLOT")
            self.assertFalse(combined_followups)
        finally:
            shutil.rmtree(data, ignore_errors=True)

    def test_practice_day_summary_includes_zero_days_and_omits_unassigned_teams(self):
        from team_builder.cli import practice_day_summary

        self.assertEqual(
            practice_day_summary([
                {"slot_id": "slot-1", "day": "Monday"},
                {"slot_id": "slot-2", "day": "Monday"},
                {"slot_id": "slot-3", "day": "Thursday"},
                {"slot_id": "", "day": "Friday"},
            ]),
            "M: 2, T: 0, W: 0, R: 1, F: 0",
        )

    def test_normalization_and_small_end_to_end_run(self):
        OUTPUT.mkdir(parents=True, exist_ok=True)
        stale_report = OUTPUT / "99_stale_report.csv"
        unrelated_file = OUTPUT / "coordinator-notes.txt"
        stale_report.write_text("stale\n", encoding="utf-8")
        unrelated_file.write_text("retain\n", encoding="utf-8")
        result = Pipeline("4th", FIXTURES, OUTPUT, CONFIG).run()
        self.assertEqual(result, {"players": 3, "teams": 1, "unassigned": 0})
        self.assertFalse(stale_report.exists())
        self.assertTrue(unrelated_file.exists())
        with (OUTPUT / "01_players_normalized.csv").open(newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[0]["Monday_early"], "yes")
        self.assertEqual(rows[1]["Monday_late"], "yes")
        with (OUTPUT / "02_request_resolution.csv").open(newline="") as fh:
            requests = list(csv.DictReader(fh))
        self.assertEqual(requests[0]["status"], "resolved")
        with (OUTPUT / "04_unresolved_friend_requests.csv").open(newline="") as fh:
            unresolved_reader = csv.DictReader(fh)
            list(unresolved_reader)
        self.assertEqual(
            unresolved_reader.fieldnames,
            ["request_id", "reg_number", "player_name", "submitter_name", "submitter_email", "requested_name"],
        )
        with (OUTPUT / "04_friend_request_analysis.csv").open(newline="") as fh:
            friend_analysis = list(csv.DictReader(fh))
        self.assertEqual(friend_analysis[0]["player_name"], "Ann Alpha")
        self.assertIn("submitter_email", friend_analysis[0])
        with (OUTPUT / "08_unmet_requests.csv").open(newline="") as fh:
            unmet_reader = csv.DictReader(fh)
            list(unmet_reader)
        self.assertIn("submitter_email", unmet_reader.fieldnames)
        self.assertIn("unmet_reason", unmet_reader.fieldnames)
        self.assertIn("source_availability", unmet_reader.fieldnames)
        self.assertIn("requested_availability", unmet_reader.fieldnames)
        self.assertIn("recommended_action", unmet_reader.fieldnames)
        self.assertTrue((OUTPUT / "08_final_validation.csv").exists())
        with (OUTPUT / "08_players_no_request_met.csv").open(newline="") as fh:
            no_met = csv.DictReader(fh)
            no_met_rows = list(no_met)
        self.assertIn("primary_reason", no_met.fieldnames)
        self.assertTrue(all(r["request_count"] != "0" for r in no_met_rows))
        with (OUTPUT / "09_assigned_practice_slots.csv").open(newline="") as fh:
            assigned_slots = list(csv.DictReader(fh))
        self.assertEqual(
            list(assigned_slots[0]),
            ["Location", "Day", "Time", "Team", "Head Coach"],
        )
        self.assertEqual(assigned_slots[0]["Team"], "4th-01")
        self.assertEqual(assigned_slots[0]["Head Coach"], "Coach One")

    def test_coach_daughter_and_friends_anchor_the_coach_team(self):
        data = Path(tempfile.mkdtemp())
        out = data / "out"
        either = "Available either slot"
        none = "Unavailable"
        (data / "practice-slots.csv").write_text("Day,Time,Location\nMonday,Early,G\nMonday,Late,G\n", encoding="utf-8")
        (data / "coaches.csv").write_text(
            "Volunteer First Name,Volunteer Last Name,Volunteer Email,Group Name,Volunteer Role,UUID,RegChild,Requests,Monday_1,Tuesday_1,Wednesday_1,Thursday_1,Friday_1\n"
            f"Cora,Coach,cora@example.test,Age Group >4th Grade,Head Coach,c-a,Yes,,{either},{none},{none},{none},{none}\n"
            f"Bo,Boss,bo@example.test,Age Group >4th Grade,Head Coach,c-b,,,{either},{none},{none},{none},{none}\n", encoding="utf-8")
        header = "Player First Name,Player Last Name,Reg Number,Group Name,UUID,Submitter Email,FriendRequests,CoachRequest,Monday,Tuesday,Wednesday,Thursday,Friday\n"
        rows = [("Dana", "Dee", "101", "cora@example.test", "Fern Fay"), ("Fern", "Fay", "102", "f@example.test", ""),
                ("Gail", "Gee", "103", "g@example.test", "Dana Dee"), ("Hope", "Hay", "104", "h@example.test", ""),
                ("Iris", "Ivy", "105", "i@example.test", ""), ("Jan", "Jay", "106", "j@example.test", "")]
        (data / "players.csv").write_text(header + "".join(
            f"{a},{b},{c},Age Group >4th Grade,u{c},{e},{f},,{either},{none},{none},{none},{none}\n" for a, b, c, e, f in rows), encoding="utf-8")
        config = {**CONFIG, "team_counts": {"4th": 2}, "fixed_team_heads": {"4th": {"4th-01": "c-b", "4th-02": "c-a"}}}
        try:
            Pipeline("4th", data, out, config).run()
            with (out / "07_player_assignments.csv").open(newline="") as fh:
                team = {r["reg_number"]: r["team_id"] for r in csv.DictReader(fh)}
            self.assertEqual({team["101"], team["102"], team["103"]}, {"4th-02"})
            with (out / "05_anchor_teams.csv").open(newline="") as fh:
                anchor = list(csv.DictReader(fh))
            self.assertEqual(sorted(r["relationship"] for r in anchor), ["daughter", "requested_by_daughter", "requested_daughter"])
            self.assertTrue(all(r["outcome"] == "on_team" for r in anchor))
        finally:
            shutil.rmtree(data, ignore_errors=True)

    def test_coach_child_friend_cluster_locks_a_compatible_slot(self):
        data = Path(tempfile.mkdtemp())
        out = data / "out"
        either, none = "Available either slot", "Unavailable"
        tue = "Available early only - 5:30 pm to 7:30 pm"
        mon = "Available early only - 5:30 pm to 7:30 pm"
        (data / "practice-slots.csv").write_text("Day,Time,Location\nMonday,Early,G\nTuesday,Early,G\n", encoding="utf-8")
        (data / "coaches.csv").write_text(
            "Volunteer First Name,Volunteer Last Name,Volunteer Email,Group Name,Volunteer Role,UUID,RegChild,Requests,Monday_1,Tuesday_1,Wednesday_1,Thursday_1,Friday_1\n"
            f"Cora,Coach,cora@example.test,Age Group >4th Grade,Head Coach,c-a,Yes,,{either},{either},{none},{none},{none}\n"
            f"Bo,Boss,bo@example.test,Age Group >4th Grade,Head Coach,c-b,,,{either},{either},{none},{none},{none}\n",
            encoding="utf-8",
        )
        header = "Player First Name,Player Last Name,Reg Number,Group Name,UUID,Submitter Email,FriendRequests,CoachRequest,Monday,Tuesday,Wednesday,Thursday,Friday\n"
        rows = [
            ("Kid", "Coach", "1", "cora@example.test", "Pal Out", "Cora Coach", tue, tue),
            ("Pal", "In", "2", "in@example.test", "Kid Coach", "Cora Coach", none, tue),
            ("Pal", "Out", "3", "out@example.test", "", "Cora Coach", none, tue),
            ("Solo", "Player", "4", "solo@example.test", "", "Cora Coach; Bo Boss", mon, none),
        ]
        (data / "players.csv").write_text(header + "".join(
            f"{first},{last},{number},Age Group >4th Grade,u{number},{email},{friends},{coach},{monday},{tuesday},{none},{none},{none}\n"
            for first, last, number, email, friends, coach, monday, tuesday in rows
        ), encoding="utf-8")
        config = {
            **CONFIG,
            "team_counts": {"4th": 2},
            "roster": {"minimum": 1, "target": 2, "maximum": 3},
            "fixed_team_heads": {"4th": {"4th-01": "c-a", "4th-02": "c-b"}},
        }
        try:
            Pipeline("4th", data, out, config).run()
            with (out / "05_team_slots.csv").open(newline="") as fh:
                slots = {row["team_id"]: row for row in csv.DictReader(fh)}
            self.assertEqual(slots["4th-01"]["day"], "Tuesday")
            with (out / "07_player_assignments.csv").open(newline="") as fh:
                assignments = {row["reg_number"]: row for row in csv.DictReader(fh)}
            self.assertEqual({assignments[pid]["team_id"] for pid in ("1", "2", "3")}, {"4th-01"})
            self.assertTrue(all(assignments[pid]["placement_stage"] == "anchor" for pid in ("1", "2", "3")))
            with (out / "02_request_resolution.csv").open(newline="") as fh:
                requests = {row["source_reg_number"]: row for row in csv.DictReader(fh) if row["request_type"] == "coach"}
            self.assertEqual(requests["1"]["status"], "resolved")
            self.assertEqual(requests["4"]["status"], "invalid")
            self.assertEqual(requests["4"]["reason"], "coach requests must name exactly one coach")
        finally:
            shutil.rmtree(data, ignore_errors=True)

    def test_unmet_coach_request_reports_team_slot_incompatibility(self):
        data = Path(tempfile.mkdtemp())
        out = data / "out"
        early = "Available early only - 5:30 pm to 7:30 pm"
        late = "Available late only - 7:00 pm to 9:00 pm"
        none = "Unavailable"
        (data / "practice-slots.csv").write_text(
            "Day,Time,Location\nMonday,Early,G\nMonday,Late,G\nTuesday,Late,G\n",
            encoding="utf-8",
        )
        (data / "coaches.csv").write_text(
            "Volunteer First Name,Volunteer Last Name,Volunteer Email,Group Name,Volunteer Role,UUID,RegChild,Requests,Monday_1,Tuesday_1,Wednesday_1,Thursday_1,Friday_1\n"
            f"Cora,Coach,cora@example.test,Age Group >4th Grade,Head Coach,c-a,Yes,,{late},{none},{none},{none},{none}\n"
            f"Bo,Boss,bo@example.test,Age Group >4th Grade,Head Coach,c-b,,,{early},{none},{none},{none},{none}\n"
            f"Dee,Director,dee@example.test,Age Group >4th Grade,Head Coach,c-c,,,{none},{late},{none},{none},{none}\n",
            encoding="utf-8",
        )
        header = "Player First Name,Player Last Name,Reg Number,Group Name,UUID,Submitter Email,FriendRequests,CoachRequest,Monday,Tuesday,Wednesday,Thursday,Friday\n"
        rows = [
            ("Kid", "Coach", "1", "cora@example.test", "Pal One; Pal Two", "", late, none),
            ("Pal", "One", "2", "one@example.test", "", "", late, none),
            ("Pal", "Two", "3", "two@example.test", "", "", late, none),
            ("Early", "Requester", "4", "early@example.test", "", "Cora Coach", early, none),
            ("Late", "Requester", "5", "late@example.test", "", "Cora Coach", late, late),
        ]
        (data / "players.csv").write_text(header + "".join(
            f"{first},{last},{number},Age Group >4th Grade,u{number},{email},{friends},{coach},{monday},{tuesday},{none},{none},{none}\n"
            for first, last, number, email, friends, coach, monday, tuesday in rows
        ), encoding="utf-8")
        config = {
            **CONFIG,
            "team_counts": {"4th": 3},
            "roster": {"minimum": 1, "target": 2, "maximum": 3},
            "fixed_team_heads": {"4th": {"4th-01": "c-a", "4th-02": "c-b", "4th-03": "c-c"}},
        }
        try:
            Pipeline("4th", data, out, config).run()
            with (out / "08_unmet_requests.csv").open(newline="") as fh:
                reasons = {
                    row["source_reg_number"]: row["unmet_reason"]
                    for row in csv.DictReader(fh)
                    if row["request_type"] == "coach"
                }
            self.assertEqual(reasons["4"], "COACH_TEAM_SLOT_UNAVAILABLE")
            self.assertEqual(reasons["5"], "COACH_TEAM_FULL")
            with (out / "08_players_no_request_met.csv").open(newline="") as fh:
                primary_reasons = {row["reg_number"]: row["primary_reason"] for row in csv.DictReader(fh)}
            self.assertEqual(primary_reasons["4"], "COACH_TEAM_SLOT_UNAVAILABLE")
            self.assertEqual(primary_reasons["5"], "COACH_TEAM_FULL")
        finally:
            shutil.rmtree(data, ignore_errors=True)

    def test_unmet_coach_request_reason_uses_generic_fallback_for_non_full_team(self):
        player = {"Monday_early": "yes", "Monday_late": "no"}
        team = {"team_id": "4th-01", "day": "Monday", "time": "Early"}

        self.assertEqual(
            unmet_coach_request_reason(player, [team], {"4th-01": ["1", "2"]}, 3),
            "COACH_ASSIGNED_TO_DIFFERENT_TEAM",
        )

    def test_mutual_friend_pair_is_placed_before_general_clusters(self):
        data = Path(tempfile.mkdtemp())
        out = data / "out"
        early = "Available early only - 5:30 pm to 7:30 pm"
        none = "Unavailable"
        (data / "practice-slots.csv").write_text(
            "Day,Time,Location\nMonday,Early,G\nTuesday,Early,G\n",
            encoding="utf-8",
        )
        (data / "coaches.csv").write_text(
            "Volunteer First Name,Volunteer Last Name,Group Name,Volunteer Role,UUID,RegChild,Requests,Monday_1,Tuesday_1,Wednesday_1,Thursday_1,Friday_1\n"
            f"Cora,Coach,Age Group >4th Grade,Head Coach,c-a,,,{early},{none},{none},{none},{none}\n"
            f"Bo,Boss,Age Group >4th Grade,Head Coach,c-b,,,{none},{early},{none},{none},{none}\n",
            encoding="utf-8",
        )
        header = "Player First Name,Player Last Name,Reg Number,Group Name,UUID,FriendRequests,CoachRequest,Monday,Tuesday,Wednesday,Thursday,Friday\n"
        rows = [
            ("Mutual", "One", "1", "Mutual Two", none, early),
            ("Mutual", "Two", "2", "Mutual One", none, early),
            ("Solo", "Monday", "3", "", early, none),
            ("Solo", "Monday Two", "4", "", early, none),
        ]
        (data / "players.csv").write_text(header + "".join(
            f"{first},{last},{number},Age Group >4th Grade,u{number},{friends},,{monday},{tuesday},{none},{none},{none}\n"
            for first, last, number, friends, monday, tuesday in rows
        ), encoding="utf-8")
        config = {
            **CONFIG,
            "team_counts": {"4th": 2},
            "roster": {"minimum": 1, "target": 2, "maximum": 3},
            "fixed_team_heads": {"4th": {"4th-01": "c-a", "4th-02": "c-b"}},
        }
        try:
            Pipeline("4th", data, out, config).run()
            with (out / "07_player_assignments.csv").open(newline="") as fh:
                assignments = {row["reg_number"]: row for row in csv.DictReader(fh)}
            self.assertEqual({assignments["1"]["team_id"], assignments["2"]["team_id"]}, {"4th-02"})
            self.assertTrue(all(assignments[pid]["placement_stage"] == "mutual_pair" for pid in ("1", "2")))
        finally:
            shutil.rmtree(data, ignore_errors=True)

    def test_mutual_pair_slot_coverage_outranks_broad_player_coverage(self):
        data = Path(tempfile.mkdtemp())
        out = data / "out"
        either = "Available either slot"
        early = "Available early only - 5:30 pm to 7:30 pm"
        late = "Available late only - 7:00 pm to 9:00 pm"
        none = "Unavailable"
        (data / "practice-slots.csv").write_text(
            "Day,Time,Location\nMonday,Early,Gym A\nMonday,Early,Gym B\nMonday,Late,Gym A\nMonday,Late,Gym B\n",
            encoding="utf-8",
        )
        (data / "coaches.csv").write_text(
            "Volunteer First Name,Volunteer Last Name,Group Name,Volunteer Role,UUID,RegChild,Requests,Monday_1,Tuesday_1,Wednesday_1,Thursday_1,Friday_1\n"
            f"Al,Ace,Age Group >4th Grade,Head Coach,c-a,,,{either},{none},{none},{none},{none}\n"
            f"Bo,Boss,Age Group >4th Grade,Head Coach,c-b,,,{either},{none},{none},{none},{none}\n",
            encoding="utf-8",
        )
        header = "Player First Name,Player Last Name,Reg Number,Group Name,UUID,FriendRequests,CoachRequest,Monday,Tuesday,Wednesday,Thursday,Friday\n"
        players = [
            ("Late", "One", "1", "Late Two", late),
            ("Late", "Two", "2", "Late One", late),
            ("Early", "One", "3", "", early),
            ("Early", "Two", "4", "", early),
            ("Early", "Three", "5", "", early),
        ]
        (data / "players.csv").write_text(
            header
            + "".join(
                f"{first},{last},{number},Age Group >4th Grade,u{number},{friends},,{monday},{none},{none},{none},{none}\n"
                for first, last, number, friends, monday in players
            ),
            encoding="utf-8",
        )
        config = {
            **CONFIG,
            "team_counts": {"4th": 2},
            "roster": {"minimum": 1, "target": 3, "maximum": 3},
            "fixed_team_heads": {"4th": {"4th-01": "c-a", "4th-02": "c-b"}},
        }
        try:
            Pipeline("4th", data, out, config).run()
            with (out / "05_team_slots.csv").open(newline="") as fh:
                slots = {row["team_id"]: row for row in csv.DictReader(fh)}
            with (out / "07_player_assignments.csv").open(newline="") as fh:
                assignments = {row["reg_number"]: row for row in csv.DictReader(fh)}
            self.assertIn("Late", {slot["time"] for slot in slots.values()})
            self.assertEqual(assignments["1"]["team_id"], assignments["2"]["team_id"])
        finally:
            shutil.rmtree(data, ignore_errors=True)

    def test_friend_pass_keeps_pairs_together_without_stranding_players(self):
        data = Path(tempfile.mkdtemp())
        out = data / "out"
        either, early, none = "Available either slot", "Available early only - 5:30 pm to 7:30 pm", "Unavailable"
        (data / "practice-slots.csv").write_text("Day,Time,Location\nMonday,Early,G\nMonday,Late,G\n", encoding="utf-8")
        (data / "coaches.csv").write_text(
            "Volunteer First Name,Volunteer Last Name,Group Name,Volunteer Role,UUID,RegChild,Requests,Monday_1,Tuesday_1,Wednesday_1,Thursday_1,Friday_1\n"
            f"Al,Ace,Age Group >4th Grade,Head Coach,c-a,,,{either},{none},{none},{none},{none}\n"
            f"Bo,Boss,Age Group >4th Grade,Head Coach,c-b,,,{either},{none},{none},{none},{none}\n", encoding="utf-8")
        header = "Player First Name,Player Last Name,Reg Number,Group Name,UUID,FriendRequests,CoachRequest,Monday,Tuesday,Wednesday,Thursday,Friday\n"
        rows = [("Ann", "Aa", "1", "Bea Bb", either), ("Bea", "Bb", "2", "", either), ("Cy", "Cc", "3", "", early), ("Di", "Dd", "4", "", early)]
        (data / "players.csv").write_text(header + "".join(
            f"{a},{b},{c},Age Group >4th Grade,u{c},{f},,{av},{none},{none},{none},{none}\n" for a, b, c, f, av in rows), encoding="utf-8")
        config = {**CONFIG, "team_counts": {"4th": 2}, "roster": {"minimum": 1, "target": 2, "maximum": 2}, "fixed_team_heads": {"4th": {"4th-01": "c-a", "4th-02": "c-b"}}}
        try:
            Pipeline("4th", data, out, config).run()
            with (out / "07_player_assignments.csv").open(newline="") as fh:
                rows_out = {r["reg_number"]: r for r in csv.DictReader(fh)}
            self.assertEqual(rows_out["1"]["team_id"], rows_out["2"]["team_id"])
            self.assertEqual(rows_out["1"]["placement_stage"], "cluster")
            self.assertTrue(all(r["assignment_status"] == "assigned" for r in rows_out.values()))
        finally:
            shutil.rmtree(data, ignore_errors=True)

    def test_friend_requests_prioritize_each_requester_with_a_match(self):
        data = Path(tempfile.mkdtemp())
        out = data / "out"
        either, none = "Available either slot", "Unavailable"
        (data / "practice-slots.csv").write_text(
            "Day,Time,Location\nMonday,Early,Gym A\nMonday,Early,Gym B\n",
            encoding="utf-8",
        )
        (data / "coaches.csv").write_text(
            "Volunteer First Name,Volunteer Last Name,Group Name,Volunteer Role,UUID,RegChild,Requests,Monday_1,Tuesday_1,Wednesday_1,Thursday_1,Friday_1\n"
            f"Al,Ace,Age Group >4th Grade,Head Coach,c-a,,,{either},{none},{none},{none},{none}\n"
            f"Bo,Boss,Age Group >4th Grade,Head Coach,c-b,,,{either},{none},{none},{none},{none}\n",
            encoding="utf-8",
        )
        header = "Player First Name,Player Last Name,Reg Number,Group Name,UUID,FriendRequests,CoachRequest,Monday,Tuesday,Wednesday,Thursday,Friday\n"
        players = [
            ("Lauren", "Leader", "1", "Natalie Requester; Elise Friend; Lily Friend"),
            ("Natalie", "Requester", "2", "Maisy Friend"),
            ("Elise", "Friend", "3", ""),
            ("Lily", "Friend", "4", ""),
            ("Maisy", "Friend", "5", ""),
        ]
        (data / "players.csv").write_text(
            header
            + "".join(
                f"{first},{last},{number},Age Group >4th Grade,u{number},{friends},,{either},{none},{none},{none},{none}\n"
                for first, last, number, friends in players
            ),
            encoding="utf-8",
        )
        config = {
            **CONFIG,
            "team_counts": {"4th": 2},
            "roster": {"minimum": 1, "target": 3, "maximum": 4},
            "fixed_team_heads": {"4th": {"4th-01": "c-a", "4th-02": "c-b"}},
        }
        try:
            Pipeline("4th", data, out, config).run()
            with (out / "07_player_assignments.csv").open(newline="") as fh:
                team = {row["reg_number"]: row["team_id"] for row in csv.DictReader(fh)}
            self.assertEqual(team["2"], team["5"])
            self.assertTrue(team["1"] == team["3"] or team["1"] == team["4"])
            self.assertNotEqual(team["1"], team["2"])
        finally:
            shutil.rmtree(data, ignore_errors=True)

    def test_friend_cluster_splits_along_slot_lines_with_bridge_player(self):
        data = Path(tempfile.mkdtemp())
        out = data / "out"
        either, none = "Available either slot", "Unavailable"
        mon = "Available early only - 5:30 pm to 7:30 pm"
        (data / "practice-slots.csv").write_text("Day,Time,Location\nMonday,Early,G\nTuesday,Early,G\n", encoding="utf-8")
        (data / "coaches.csv").write_text(
            "Volunteer First Name,Volunteer Last Name,Group Name,Volunteer Role,UUID,RegChild,Requests,Monday_1,Tuesday_1,Wednesday_1,Thursday_1,Friday_1\n"
            f"Al,Ace,Age Group >4th Grade,Head Coach,c-a,,,{either},{either},{none},{none},{none}\n"
            f"Bo,Boss,Age Group >4th Grade,Head Coach,c-b,,,{either},{either},{none},{none},{none}\n", encoding="utf-8")
        header = "Player First Name,Player Last Name,Reg Number,Group Name,UUID,FriendRequests,CoachRequest,Monday,Tuesday,Wednesday,Thursday,Friday\n"
        players = [("A", "Mon", "1", "B Mon; Hub Bridge", mon, none), ("B", "Mon", "2", "A Mon", mon, none), ("C", "Tue", "3", "D Tue; Hub Bridge", none, mon),
                   ("D", "Tue", "4", "C Tue", none, mon), ("Hub", "Bridge", "5", "", either, either), ("E", "Fill", "6", "", either, either)]
        (data / "players.csv").write_text(header + "".join(
            f"{a},{b},{c},Age Group >4th Grade,u{c},{f},,{m},{tu},{none},{none},{none}\n" for a, b, c, f, m, tu in players), encoding="utf-8")
        config = {**CONFIG, "team_counts": {"4th": 2}, "roster": {"minimum": 1, "target": 3, "maximum": 3}, "fixed_team_heads": {"4th": {"4th-01": "c-a", "4th-02": "c-b"}}}
        try:
            Pipeline("4th", data, out, config).run()
            with (out / "07_player_assignments.csv").open(newline="") as fh:
                team = {r["reg_number"]: r["team_id"] for r in csv.DictReader(fh)}
            self.assertEqual(team["1"], team["2"])
            self.assertEqual(team["3"], team["4"])
            self.assertNotEqual(team["1"], team["3"])
            with (out / "07_friend_clusters.csv").open(newline="") as fh:
                clusters = list(csv.DictReader(fh))
            self.assertEqual(len(clusters), 5)
            self.assertTrue(all(r["can_attend_team_slot"] == "yes" for r in clusters))
        finally:
            shutil.rmtree(data, ignore_errors=True)

    def test_practice_override_accepts_day_time_location(self):
        data = Path(tempfile.mkdtemp())
        out = data / "out"
        for name in ("players.csv", "coaches.csv", "practice-slots.csv"):
            shutil.copy(FIXTURES / name, data / name)
        (data / "ov.csv").write_text("constraint_type,subject_id,target_id,value,notes\nassign_practice_slot,4th-01,Monday | Late | Gym A,,spring\n", encoding="utf-8")
        try:
            Pipeline("4th", data, out, CONFIG, data / "ov.csv").run()
            with (out / "05_team_slots.csv").open(newline="") as fh:
                row = next(csv.DictReader(fh))
            self.assertEqual((row["day"], row["time"], row["location"]), ("Monday", "Late", "Gym A"))
        finally:
            shutil.rmtree(data, ignore_errors=True)

    def test_multiple_assistant_coaches_per_team_are_allowed(self):
        data = Path(tempfile.mkdtemp())
        out = data / "out"
        for name in ("players.csv", "practice-slots.csv"):
            shutil.copy(FIXTURES / name, data / name)
        either, none = "Available either slot", "Unavailable"
        (data / "coaches.csv").write_text(
            "Volunteer First Name,Volunteer Last Name,Group Name,Volunteer Role,UUID,RegChild,Requests,Monday_1,Tuesday_1,Wednesday_1,Thursday_1,Friday_1\n" + "".join(
                f"C{i},Coach,Age Group >4th Grade,{'Head Coach' if i == 1 else 'Assistant Coach'},c-{i},,,{either},{none},{none},{none},{none}\n" for i in range(1, 5)), encoding="utf-8")
        (data / "ov.csv").write_text("constraint_type,subject_id,target_id,value,notes\n" + "".join(f"assign_co_head_coach,4th-01,c-{i},,x\n" for i in (2, 3, 4)), encoding="utf-8")
        try:
            Pipeline("4th", data, out, CONFIG, data / "ov.csv").run()
            with (out / "03_constraints.csv").open(newline="") as fh:
                self.assertTrue(all(r["status"] == "valid" for r in csv.DictReader(fh)))
            with (out / "05_coach_assignments.csv").open(newline="") as fh:
                self.assertEqual(len(list(csv.DictReader(fh))), 4)
        finally:
            shutil.rmtree(data, ignore_errors=True)

    def test_assistant_with_linked_daughter_and_requesters_shape_the_slot(self):
        data = Path(tempfile.mkdtemp())
        out = data / "out"
        either, none = "Available either slot", "Unavailable"
        fri = "Available early only - 5:30 pm to 7:30 pm"
        (data / "practice-slots.csv").write_text("Day,Time,Location\nMonday,Early,G\nFriday,Early,G\n", encoding="utf-8")
        (data / "coaches.csv").write_text(
            "Volunteer First Name,Volunteer Last Name,Group Name,Volunteer Role,UUID,RegChild,Requests,Monday_1,Tuesday_1,Wednesday_1,Thursday_1,Friday_1\n"
            f"Hd,Head,Age Group >4th Grade,Head Coach,c-h,,,{either},{none},{none},{none},{either}\n"
            f"As,Assist,Age Group >4th Grade,Assistant Coach,c-a,Yes,,{none},{none},{none},{none},{fri}\n", encoding="utf-8")
        header = "Player First Name,Player Last Name,Reg Number,Group Name,UUID,FriendRequests,CoachRequest,Monday,Tuesday,Wednesday,Thursday,Friday\n"
        rows = [("Kid", "One", "1", ""), ("Pal", "Two", "2", ""), ("Pal", "Three", "3", "")]
        (data / "players.csv").write_text(header + "".join(
            f"{a},{b},{c},Age Group >4th Grade,u{c},{f},,{either},{none},{none},{none},{either}\n" for a, b, c, f in rows), encoding="utf-8")
        (data / "ov.csv").write_text("constraint_type,subject_id,target_id,value,notes\nassign_head_coach,4th-01,c-h,,h\nassign_co_head_coach,4th-01,c-a,,a\nlink_coach_daughter,c-a,1,,link\n", encoding="utf-8")
        try:
            Pipeline("4th", data, out, {**CONFIG, "fixed_team_heads": {}}, data / "ov.csv").run()
            with (out / "05_team_slots.csv").open(newline="") as fh:
                row = next(csv.DictReader(fh))
            self.assertEqual(row["day"], "Friday")
            with (out / "07_player_assignments.csv").open(newline="") as fh:
                self.assertEqual({r["reg_number"]: r["placement_stage"] for r in csv.DictReader(fh)}["1"], "anchor")
        finally:
            shutil.rmtree(data, ignore_errors=True)

    def test_anchor_group_may_fill_team_to_roster_maximum(self):
        data = Path(tempfile.mkdtemp())
        out = data / "out"
        either, none = "Available either slot", "Unavailable"
        (data / "practice-slots.csv").write_text("Day,Time,Location\nMonday,Early,G\nMonday,Late,G\n", encoding="utf-8")
        (data / "coaches.csv").write_text(
            "Volunteer First Name,Volunteer Last Name,Volunteer Email,Group Name,Volunteer Role,UUID,RegChild,Requests,Monday_1,Tuesday_1,Wednesday_1,Thursday_1,Friday_1\n"
            f"Cora,Coach,cora@example.test,Age Group >4th Grade,Head Coach,c-a,Yes,,{either},{none},{none},{none},{none}\n"
            f"Bo,Boss,bo@example.test,Age Group >4th Grade,Head Coach,c-b,,,{either},{none},{none},{none},{none}\n", encoding="utf-8")
        header = "Player First Name,Player Last Name,Reg Number,Group Name,UUID,Submitter Email,FriendRequests,CoachRequest,Monday,Tuesday,Wednesday,Thursday,Friday\n"
        rows = [("Dana", "Dee", "1", "cora@example.test", "Pal Two; Pal Three; Pal Four"), ("Pal", "Two", "2", "p2@example.test", ""), ("Pal", "Three", "3", "p3@example.test", ""),
                ("Pal", "Four", "4", "p4@example.test", ""), ("Zed", "Five", "5", "p5@example.test", ""), ("Yan", "Six", "6", "p6@example.test", "")]
        (data / "players.csv").write_text(header + "".join(
            f"{a},{b},{c},Age Group >4th Grade,u{c},{e},{f},,{either},{none},{none},{none},{none}\n" for a, b, c, e, f in rows), encoding="utf-8")
        config = {**CONFIG, "team_counts": {"4th": 2}, "roster": {"minimum": 2, "target": 3, "maximum": 4}, "fixed_team_heads": {"4th": {"4th-01": "c-a", "4th-02": "c-b"}}}
        try:
            Pipeline("4th", data, out, config).run()
            with (out / "07_player_assignments.csv").open(newline="") as fh:
                team = {r["reg_number"]: r["team_id"] for r in csv.DictReader(fh)}
            self.assertEqual({team[x] for x in "1234"}, {"4th-01"})
            self.assertEqual({team["5"], team["6"]}, {"4th-02"})
            with (out / "05_anchor_team_summary.csv").open(newline="") as fh:
                summary = {r["team_id"]: r for r in csv.DictReader(fh)}
            self.assertEqual(summary["4th-01"]["final_roster_size"], "4")
            self.assertEqual(summary["4th-01"]["spots_left_to_max"], "0")
        finally:
            shutil.rmtree(data, ignore_errors=True)

    def test_coach_daughter_linked_by_child_name(self):
        data = Path(tempfile.mkdtemp())
        out = data / "out"
        either, none = "Available either slot", "Unavailable"
        (data / "practice-slots.csv").write_text("Day,Time,Location\nMonday,Early,G\n", encoding="utf-8")
        (data / "coaches.csv").write_text(
            "Volunteer First Name,Volunteer Last Name,Volunteer Email,Group Name,Volunteer Role,UUID,RegChild,ChildName,Requests,Monday_1,Tuesday_1,Wednesday_1,Thursday_1,Friday_1\n"
            f"Cora,Coach,cora@example.test,Age Group >4th Grade,Head Coach,c-1,Yes,Kid (Kiddo) Coach,,{either},{none},{none},{none},{none}\n", encoding="utf-8")
        header = "Player First Name,Player Last Name,Reg Number,Group Name,UUID,Submitter Email,FriendRequests,CoachRequest,Monday,Tuesday,Wednesday,Thursday,Friday\n"
        rows = [("Kid", "Coach", "1", "mom@example.test"), ("Sib", "Coach", "2", "cora@example.test")]
        (data / "players.csv").write_text(header + "".join(
            f"{a},{b},{c},Age Group >4th Grade,u{c},{e},,,{either},{none},{none},{none},{none}\n" for a, b, c, e in rows), encoding="utf-8")
        try:
            Pipeline("4th", data, out, CONFIG).run()
            with (out / "05_coach_child_links.csv").open(newline="") as fh:
                links = list(csv.DictReader(fh))
            self.assertEqual([(r["method"], r["status"], r["matched_reg_numbers"]) for r in links], [("child_name", "linked", "1")])
        finally:
            shutil.rmtree(data, ignore_errors=True)

    def test_low_availability_coach_requester_protected_and_reported(self):
        data = Path(tempfile.mkdtemp())
        out = data / "out"
        either, none = "Available either slot", "Unavailable"
        early = "Available early only - 5:30 pm to 7:30 pm"
        late = "Available late only - 7:00 pm to 9:00 pm"
        (data / "practice-slots.csv").write_text("Day,Time,Location\nMonday,Early,G\nMonday,Late,G\n", encoding="utf-8")
        (data / "coaches.csv").write_text(
            "Volunteer First Name,Volunteer Last Name,Group Name,Volunteer Role,UUID,RegChild,Requests,Monday_1,Tuesday_1,Wednesday_1,Thursday_1,Friday_1\n"
            f"Al,Ace,Age Group >4th Grade,Head Coach,c-a,,,{either},{none},{none},{none},{none}\n"
            f"Bo,Boss,Age Group >4th Grade,Head Coach,c-b,,,{late},{none},{none},{none},{none}\n", encoding="utf-8")
        header = "Player First Name,Player Last Name,Reg Number,Group Name,UUID,FriendRequests,CoachRequest,Monday,Tuesday,Wednesday,Thursday,Friday\n"
        rows = [("Lo", "Few", "1", early, none), ("Pat", "Late", "2", late, none), ("Sam", "Late", "3", late, none)]
        (data / "players.csv").write_text(header + "".join(
            f"{a},{b},{c},Age Group >4th Grade,u{c},,Al Ace,{m},{none},{none},{none},{none}\n" for a, b, c, m, _ in rows), encoding="utf-8")
        try:
            Pipeline("4th", data, out, {**CONFIG, "team_counts": {"4th": 2}, "roster": {"minimum": 1, "target": 3, "maximum": 5}, "fixed_team_heads": {"4th": {"4th-01": "c-a", "4th-02": "c-b"}}}).run()
            with (out / "05_team_slots.csv").open(newline="") as fh:
                self.assertEqual(next(csv.DictReader(fh))["time"], "Early")
            with (out / "08_low_availability_requesters.csv").open(newline="") as fh:
                reported = {r["reg_number"]: r["open_slots_of_10"] for r in csv.DictReader(fh)}
            self.assertEqual(reported, {"1": "1", "2": "1", "3": "1"})
        finally:
            shutil.rmtree(data, ignore_errors=True)

    def test_low_availability_player_without_requests_yields_to_coach_request(self):
        data = Path(tempfile.mkdtemp())
        out = data / "out"
        early = "Available early only - 5:30 pm to 7:30 pm"
        late = "Available late only - 7:00 pm to 9:00 pm"
        none = "Unavailable"
        (data / "practice-slots.csv").write_text(
            "Day,Time,Location\nMonday,Early,Gym A\nMonday,Early,Gym B\nWednesday,Late,Gym A\n",
            encoding="utf-8",
        )
        (data / "coaches.csv").write_text(
            "Volunteer First Name,Volunteer Last Name,Group Name,Volunteer Role,UUID,RegChild,Requests,Monday_1,Tuesday_1,Wednesday_1,Thursday_1,Friday_1\n"
            f"Cora,Coach,Age Group >4th Grade,Head Coach,c-a,,,{early},{none},{late},{none},{none}\n"
            f"Bo,Boss,Age Group >4th Grade,Head Coach,c-b,,,{early},{none},{none},{none},{none}\n",
            encoding="utf-8",
        )
        header = "Player First Name,Player Last Name,Reg Number,Group Name,UUID,FriendRequests,CoachRequest,Monday,Tuesday,Wednesday,Thursday,Friday\n"
        rows = [
            ("Requested", "Player", "1", "Cora Coach", early, none, none, none, none),
            ("Wednesday", "Only", "2", "", none, none, late, none, none),
            ("Monday", "Filler", "3", "", early, none, none, none, none),
        ]
        (data / "players.csv").write_text(
            header
            + "".join(
                f"{first},{last},{number},Age Group >4th Grade,u{number},,{coach},{monday},{tuesday},{wednesday},{thursday},{friday}\n"
                for first, last, number, coach, monday, tuesday, wednesday, thursday, friday in rows
            ),
            encoding="utf-8",
        )
        config = {
            **CONFIG,
            "team_counts": {"4th": 2},
            "roster": {"minimum": 1, "target": 2, "maximum": 2},
            "fixed_team_heads": {"4th": {"4th-01": "c-a", "4th-02": "c-b"}},
        }
        try:
            Pipeline("4th", data, out, config).run()
            with (out / "05_team_slots.csv").open(newline="") as fh:
                slots = {row["team_id"]: row for row in csv.DictReader(fh)}
            with (out / "07_player_assignments.csv").open(newline="") as fh:
                assignments = {row["reg_number"]: row for row in csv.DictReader(fh)}
            with (out / "08_low_availability_tradeoffs.csv").open(newline="") as fh:
                tradeoffs = {row["reg_number"]: row for row in csv.DictReader(fh)}
            self.assertEqual((slots["4th-01"]["day"], slots["4th-01"]["time"]), ("Monday", "Early"))
            self.assertEqual(assignments["1"]["team_id"], "4th-01")
            self.assertEqual(assignments["2"]["assignment_status"], "unassigned")
            self.assertEqual(tradeoffs["2"]["reason"], "LOW_AVAILABILITY_PRIORITY_TRADEOFF")
        finally:
            shutil.rmtree(data, ignore_errors=True)

    def test_divisions_share_slot_pool_without_overlap(self):
        data = Path(tempfile.mkdtemp())
        either, none = "Available either slot", "Unavailable"
        early = "Available early only - 5:30 pm to 7:30 pm"
        (data / "practice-slots.csv").write_text("Day,Time,Location\nMonday,Early,G\nTuesday,Early,G\n", encoding="utf-8")
        (data / "coaches.csv").write_text(
            "Volunteer First Name,Volunteer Last Name,Group Name,Volunteer Role,UUID,RegChild,Requests,Monday_1,Tuesday_1,Wednesday_1,Thursday_1,Friday_1\n"
            f"Al,Ace,Age Group >4th Grade,Head Coach,c-a,,,{either},{either},{none},{none},{none}\n"
            f"Bo,Boss,Age Group >5th Grade,Head Coach,c-b,,,{either},{none},{none},{none},{none}\n", encoding="utf-8")
        header = "Player First Name,Player Last Name,Reg Number,Group Name,UUID,FriendRequests,CoachRequest,Monday,Tuesday,Wednesday,Thursday,Friday\n"
        (data / "players.csv").write_text(header + "".join(
            f"P,{n},{i},Age Group >{g} Grade,u{i},,,{early},{early},{none},{none},{none}\n" for i, (n, g) in enumerate([("One", "4th"), ("Two", "4th"), ("Three", "5th"), ("Four", "5th")], 1)), encoding="utf-8")
        config = {"team_counts": {"4th": 1, "5th": 1}, "age_group_labels": {"Age Group >4th Grade": "4th", "Age Group >5th Grade": "5th"},
                  "roster": {"minimum": 1, "target": 2, "maximum": 3}, "fixed_team_heads": {"4th": {"4th-01": "c-a"}, "5th": {"5th-01": "c-b"}}, "fixed_co_heads": {}}
        try:
            pipelines = [Pipeline(d, data, data / "out" / d, config) for d in ("4th", "5th")]
            run_divisions(pipelines)
            slots = {pl.division: pl.teams[0]["slot_id"] for pl in pipelines}
            self.assertEqual(len(set(slots.values())), 2)
            self.assertEqual(pipelines[1].teams[0]["day"], "Monday")
            self.assertEqual(pipelines[0].teams[0]["day"], "Tuesday")
        finally:
            shutil.rmtree(data, ignore_errors=True)

    def test_cli_runs_only_divisions_with_overrides(self):
        data = Path(tempfile.mkdtemp())
        (data / "overrides.csv").write_text("", encoding="utf-8")
        from team_builder.cli import main
        try:
            with self.assertRaises(SystemExit):
                main(["--division", "all", "--input-dir", str(data)])
            with self.assertRaises(SystemExit):
                main(["--division", "4th,5th", "--output-dir", str(data / "x"), "--input-dir", str(data)])
        finally:
            shutil.rmtree(data, ignore_errors=True)

    def test_select_forecast_excludes_players_in_isolated_output(self):
        data = Path(tempfile.mkdtemp())
        config_path = data / "config.json"
        projection_path = data / "projected-select.csv"
        counts_path = data / "forecast-team-counts.json"
        try:
            shutil.copy(FIXTURES / "coaches.csv", data / "coaches.csv")
            shutil.copy(FIXTURES / "practice-slots.csv", data / "practice-slots.csv")
            (data / "2026-4th-overrides.csv").write_text(
                "constraint_type,subject_id,target_id,value,notes\nassign_head_coach,4th-01,c-1,,head\n",
                encoding="utf-8",
            )
            config_path.write_text(json.dumps({**CONFIG, "team_counts": {"4th": 2}}), encoding="utf-8")
            projection_path.write_text("reg_number\n103\n", encoding="utf-8")
            counts_path.write_text('{"4th": 1}\n', encoding="utf-8")
            from team_builder.cli import main
            self.assertEqual(
                main([
                    "--division", "4th",
                    "--input-dir", str(data),
                    "--players-file", str(FIXTURES / "players.csv"),
                    "--config", str(config_path),
                    "--forecast-name", "select-projection",
                    "--forecast-select", str(projection_path),
                    "--forecast-team-counts", str(counts_path),
                ]),
                0,
            )
            forecast = data / "runs" / "forecasts" / "select-projection" / "4th"
            self.assertTrue(forecast.exists())
            self.assertFalse((data / "runs" / "4th").exists())
            with (forecast / "00_projected_select_exclusions.csv").open(newline="") as fh:
                self.assertEqual([row["reg_number"] for row in csv.DictReader(fh)], ["103"])
            with (forecast / "07_player_assignments.csv").open(newline="") as fh:
                self.assertNotIn("103", {row["reg_number"] for row in csv.DictReader(fh)})
        finally:
            shutil.rmtree(data, ignore_errors=True)

    def test_unanchored_team_slot_fits_largest_friend_web(self):
        data = Path(tempfile.mkdtemp())
        out = data / "out"
        either, none = "Available either slot", "Unavailable"
        early = "Available early only - 5:30 pm to 7:30 pm"
        (data / "practice-slots.csv").write_text("Day,Time,Location\nMonday,Early,G\nTuesday,Early,G\n", encoding="utf-8")
        (data / "coaches.csv").write_text(
            "Volunteer First Name,Volunteer Last Name,Group Name,Volunteer Role,UUID,RegChild,Requests,Monday_1,Tuesday_1,Wednesday_1,Thursday_1,Friday_1\n"
            f"Al,Ace,Age Group >4th Grade,Head Coach,c-a,,,{either},{either},{none},{none},{none}\n", encoding="utf-8")
        header = "Player First Name,Player Last Name,Reg Number,Group Name,UUID,FriendRequests,CoachRequest,Monday,Tuesday,Wednesday,Thursday,Friday\n"
        names = ["Ann Aa", "Bea Bb", "Cy Cc", "Di Dd"]
        rows = [(n.split()[0], n.split()[1], str(i), "; ".join(names[(i + k) % 4] for k in (0, 1)) if i < 4 else "", early if i <= 4 else none, none if i <= 4 else early) for i, n in enumerate(names, 1)]
        rows.append(("Eve", "Ee", "5", "", none, early))
        (data / "players.csv").write_text(header + "".join(
            f"{a},{b},{c},Age Group >4th Grade,u{c},{f},,{m},{tu},{none},{none},{none}\n" for a, b, c, f, m, tu in rows), encoding="utf-8")
        try:
            Pipeline("4th", data, out, {**CONFIG, "roster": {"minimum": 1, "target": 3, "maximum": 5}, "fixed_team_heads": {"4th": {"4th-01": "c-a"}}}).run()
            with (out / "05_team_slots.csv").open(newline="") as fh:
                self.assertEqual(next(csv.DictReader(fh))["day"], "Monday")
            with (out / "07_friend_clusters.csv").open(newline="") as fh:
                reader = csv.DictReader(fh)
                clusters = list(reader)
            self.assertIn("seed_team", reader.fieldnames)
            self.assertTrue(clusters); self.assertEqual({r["seed_team"] for r in clusters}, {"yes"})
        finally:
            shutil.rmtree(data, ignore_errors=True)

    def test_conflicting_and_unknown_overrides_are_not_applied(self):
        Pipeline("4th", FIXTURES, OUTPUT, CONFIG, FIXTURES / "overrides-conflicting.csv").run()
        with (OUTPUT / "03_constraints.csv").open(newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertTrue(all(row["status"] == "invalid" for row in rows if row["value"] != "config"))

if __name__ == "__main__":
    unittest.main()
