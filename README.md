# billings

Schedule and progress tracking for the May Residence whole-house remodel.

- **[SCHEDULE.md](SCHEDULE.md)**: gantt chart, milestones and task table (rendered by GitHub).
- **[Live page](https://jsharkey.github.io/billings/)** (`docs/index.html`): interactive gantt
  plus room-by-room checklists, rebuilt and published to GitHub Pages on every push to `main`.
- **[schedule.yaml](schedule.yaml)**: the source of truth. Edit this, never the generated files.

## Setup (once per clone)

```bash
pip install -r gen/requirements.txt
git config core.hooksPath .githooks
```

The pre-commit hook runs `python3 gen/generate.py` and stages the outputs. CI runs
`python3 gen/generate.py --check` and fails if the generated files are stale (e.g. after
an edit made in GitHub's web editor).

## Updating progress

**From the live page:** open a task, click squares to set each item (to do → done → ½ → to do),
then copy the **Status YAML** block at the bottom and paste it over the `status:` section at
the end of `schedule.yaml`. Set `project.as_of` and commit. Unsent edits are kept in your
browser until you reset them or the committed status changes.

**By hand:**

1. Set `project.as_of` to today.
2. Update `status:` for the areas you worked on: `done`, `todo`, `na`, or a fraction
   like `0.5`, either for a whole template (`elec_rough: 0.5`) or per item
   (`elec_rough: {boxes_low: done, homeruns: 0.25}`).
3. Add any extra work days under `calendar.extra_days.owner` (date: capacity), or
   days you won't work under `calendar.skip_days.owner`.
4. Commit. The projection updates automatically.

## How the projection works

- Effort is in crew-days, set per checklist item in `templates:` (optionally scaled
  by each area's `scale`). Remaining work = effort x (1 - progress).
- Each crew has its own calendar: the owner works Sat/Sun; the electrical (incl. low-voltage), drywall,
  paint and flooring crews work Mon-Fri (minus `holidays`) in parallel.
- Each crew works one task at a time, picking the ready task with the longest
  remaining chain to move-in. Several small tasks can share one work day. Hand-offs
  between crews take effect the next day.
- Inspections follow Boulder County rules: requested the next business day, inspected
  the day after on Mon-Thu; the energy rater needs `lead` days' notice.
- "Critical" tasks are the chain that sets the move-in date, following whatever
  held each task back (a dependency, or the crew being busy).

## ID prefixes

| Prefix | Meaning | Prefix | Meaning |
|---|---|---|---|
| `demo_` | demolition | `insl_` | insulation |
| `fram_` | framing | `dryw_` | drywall |
| `plum_` | plumbing | `tile_` | tile |
| `elec_` | electrical | `ctop_` | countertops |
| `hvac_` | heating/ventilation | `flor_` | flooring |
| `lowv_` | low-voltage | `pant_` | paint |
| `insp_` | inspections | `trim_` | doors & trim |
| `mile_` | milestones | `cabs_` | cabinets & vanities |
| `clos_` | closet rods & shelving | `glas_` | shower glass |
