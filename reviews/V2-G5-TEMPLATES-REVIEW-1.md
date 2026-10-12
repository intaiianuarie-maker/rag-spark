# V2 G5 template review — REQUEST CHANGES

The new gold-label/partition templates are honest about missing labels and do
not claim an evaluation result. I independently parsed both templates and ran
the documented partition validation against the pinned manifest: the blank form
correctly reports `ok=False`, `evaluation_ready=False`, and 869 unassigned
manifest families.

## P2 — partition scope is broader than the evaluated corpus

The example validator sets `known_families` to **every family in the global
manifest**. That manifest includes all inventoried datasets, including sources
that are not parsed or licensed for this evaluation. The gold template is
intended to label a selected subset. Requiring every one of the 869 inventory
families to be assigned to dev/holdout either forces irrelevant families into
the split or makes the input contract inconsistent with the selected gold set.

Define the evaluation-family universe explicitly (for example, the union of
eligible gold/source families for the frozen dataset), validate complete,
non-overlapping dev/holdout coverage against that universe, and document how
the universe is frozen. Keep unknown ids rejected and holdout non-empty.

The blank template also hard-codes `/root/code/Super Rag/datasets/rag-eval` as
`common_root`; make this a contributor-supplied path or a repository-relative
placeholder so the template works outside this machine.

Please update the docs/templates and submit a separate handoff. The new files
remain prep artifacts only; real G5 still needs authored gold labels, a license
decision, Q9/G4 wiring, and a real paired run. No merge/deploy is authorized.
