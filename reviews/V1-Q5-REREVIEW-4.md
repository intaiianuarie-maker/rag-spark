# V1 Q5 — REQUEST CHANGES

Independent current answer suite: 48 passed in 1.04s. Original examples now
pass, but the requested context-preserving verification strategy is still not
implemented: substring matching plus an expanded denylist remains.

1. **P1 — Attributed/contested statements still pass grounding.**
   ExtractiveGrounder.score('The launch is approved.',
   'Alice challenged the assertion: the launch is approved.') returns 1.0.
   The same claim in '“The launch is approved,” said Alice.' also returns 1.0.
   Expanded markers fixed the original denied example, not the underlying
   unsupported promotion. Implement a coherent context-preserving quotation
   mode or validated support verification as previously requested. Do not
   address this solely by appending challenged/said to the denylist. Test the
   actual answer-engine confidence/rendering behavior with these cases.

2. **P2 — Case-folded offsets are not original source offsets.**
   For source 'Straße. The launch is approved.' and claim
   'The launch is approved.', locate returns (9,31); the original slice is
   'he launch is approved.' (correct occurrence starts at 8). Casefold expands
   ß into ss. Compute offsets in original text or map normalized offsets back
   explicitly. Add Unicode-before-match and Unicode-in-match tests asserting
   citation slices equal the actual accepted source span.

No V1 acceptance/Q9, merge or deployment. Continue V1 P1; V3/V4 deferred.
