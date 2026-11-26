---
name: merge-train
description: Fold many pending requests (issues, PRs, user lists, client requests) into one batch and then a release. Use when the user says to integrate them all, fix all the tickets and release, or add something to the merge train.
---

# Merge train

1. **Collect**: fetch, then list every source the user means: open issues and PRs of each repository (new comments on closed ones too), pasted lists, requests relayed by other sessions, finished branches not yet merged. Merge duplicates; drop what `origin/main` already does.
2. **Triage** each with the project's `issue-triage` skill when it has one. Keep meaningful bugs and broadly useful features; drop niche or out-of-scope ones with a reason. Never cut something the user listed without telling them. Show the list: in, dropped (why), already done.
3. **Code first**: run what is fixable through the `issue-batch` skill: one worker per item, one builder, one review, one landing. Workers write code and run only their narrow test; the full gate, the review and the hardware pass happen once, after all code is in. A late item joins the batch instead of getting its own round.
4. **Release** with the project's `release-prep` skill for the host, the client or both:
   - a release-note fragment for every user-facing change (see the `public-copy` skill);
   - one review of the whole range from the previous release tag to the SHA to be tagged, with the notes; fix every finding first;
   - main CI green on that SHA, and the hardware checks the project asks for;
   - tag or publish only with the user's OK ("release it" in their request counts), and never move a published tag.
5. **Reply** to reporters and contributors as the project's `AGENTS.md` says: fixed and coming in the next build or released, deferred, or declined. Close what is fixed or out of scope where those rules allow it.
6. **Report** per item: released or landed (SHA), dropped or deferred (why), not verified, and who got a reply.

While a release candidate is being tested and the user asks for it, keep going in parallel: build the next candidate or test builds with the new changes, apart from what is under test. For an urgent fix the user may ask to test while CI runs; say what that left unverified.
