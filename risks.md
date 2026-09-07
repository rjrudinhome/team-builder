# Registration and Team-Building Risks

## Data integrity

- **Stale or mismatched exports:** Registration and availability updates may use different IDs, names, emails, or submission dates. Retain immutable dated source exports and merge only the latest valid update for each registration ID.
- **Unresolved names:** Typos, nicknames, duplicates, and incomplete friend or coach names can silently make intended requests unmatched. Review unmatched names during every dry run.
- **Availability changes after a request is solved:** A family may revise availability or withdraw a friend request. Rebuild from current inputs rather than preserving prior placements.

## Feasibility and capacity

- **No shared practice availability:** Identify these early, contact families, and record a confirmed resolution: corrected availability, removed request, or accepted unmet request.
- **Capacity and coach drift:** Coach withdrawals, late registrations, assistant-coach changes, or a full requested team can invalidate an otherwise feasible request.
- **Practice-slot scarcity:** A locally optimal team slot can consume a slot another grade needs. Run all grades together before accepting a schedule.
- **Select-draft uncertainty:** Middle-school forecasts before the select draft are provisional. After select players are removed, restore normal roster limits and regenerate from scratch.

## Process and communication

- **Manual overrides accumulating:** Keep overrides minimal, confirmed, and documented. Remove or review stale overrides before each final build.
- **Communication expectations:** Do not promise individual outcomes during dry runs. Explain that final requests depend on availability, capacity, coaching, select participation, and the complete registration pool.
- **Late changes after announcement:** Use early dry runs to resolve issues before teams are announced. Once requests are satisfied and teams are published, avoid changes that trigger cascading reassignment.

## Privacy

- **Private data in public materials:** Keep registration exports and named generated reports outside the public repository. Publish only source code, schemas, synthetic fixtures, and sanitized documentation.

## Weekly dry-run checklist

1. Review unmatched player, friend, and coach names.
2. Review requests with no shared practice availability.
3. Review full requested teams and coach-capacity conflicts.
4. Review low-availability players and unassigned players.
5. Confirm practice-slot uniqueness across all grades.
6. Compare source inputs and overrides with the previous run.
7. Contact families for issues that require a decision, then record only confirmed corrections in the source data or override file.
