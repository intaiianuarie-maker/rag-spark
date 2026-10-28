# REPORTER-HANDOFF.md — living memory (update after every task)

## Done
- (init 2026-10-12 coordinator)
## Lessons learned
-
## Next
-

## 2026-10-12 reporter boot
- Reports to handoffs/PROGRESS-REPORT.md every 5m: V1/V2/V3/V4 % + prev + delta.
- V1% = 50% M-cards + 50% Q-live; V2 baseline 55% (G2/G3 merged); V3 2/9; V4 2.5/8.
- Lesson: hermetic Q9 (0.9888) != live acceptance; never inflate.

## 2026-10-13 reporter v2 (post-restart)
- Old loop (pid 832221) killed; v2 loop pid 3110163, 5m cadence to PROGRESS-REPORT.md.
- Lesson: TASKS.md M/Q counts are static (only coordinator marks DONE, none marked) so V1-V4 % flat +0.0 is CORRECT for that basis, but incomplete: active backlog is micro-tickets (museR loop). v2 detail line now carries MICRO done/total + ungated % + IP/Q/GATED.
- V2/V3/V4 % still baselines (55.0/22.2/31.2) until G/A/W cards land in tracked state; flagged as estimates, not gates.
