---
name: public-copy
description: Write public text in the user's voice - Reddit posts and comments, README and GitHub repo descriptions, release-note bullets, issue and PR replies. Use whenever text will be read by people outside this chat.
---

# Public copy

1. **Read first**: the project's `AGENTS.md`, the notes READMEs (`notes.d/README.md`) for release notes, the whole issue or thread for a reply, and the user's earlier post for a follow-up. Project rules win over this file.
2. **Check what is new**: the release notes, pending fragments and what will land before publishing. Check each claim against the code or a measurement, and again when features change before posting.
3. **Voice**: English only, plain, fluent and human, as one developer writing to users. Short and direct; technical but understandable, with the usual terms for things. Friendly in replies. In the user's own tone when they sketch the text.
4. **Cut AI tells**:
   - chains of em dashes
   - "delve", "seamless", "robust", "game-changer", "revolutionary" and other hype words
   - lists padded to three, "not just X but Y"
   - emoji bullets, bold on every line
   - closing summaries, "in conclusion", offers of more help
   - stacked caveats, hedges, ifs and buts
5. **The user's draft**: when they give their text and say to keep it, change only what they asked for. "Humanize" or "more fluent" changes the wording, not the structure, facts or claims.
6. **Facts**:
   - Every number is our own measurement, with its method (setup, what was compared); reuse recorded ones, run no new tests unless asked.
   - Never claim hardware or a case as tested when it was not. Never tell users that we lack hardware or could not test; that goes in the note to the user.
   - Never present one project's measurements as another's.

## By kind

- **Reddit post**: a title that draws readers, then what it brings them and why it matters, then how it works. A deep dive keeps method and numbers; an announcement stays short. A follow-up that clarifies says what the implementation is, without retelling what was wrong.
- **README and repo description**: lead with what sets the project apart, with current numbers and screenshots. Update the description and docs together with the README.
- **Release notes**: user-facing bullets, big releases with a highlights section first; lively when the user asks, still factual. No port credits, no mod names (unless the user says to name one), no "Experimental" on shipping features. The exception for credit is a merged contributor's fragment, in the format the project's `AGENTS.md` gives.
- **Issue and PR replies**: short. Say what changed or what the cause is, what the person can do, and that it comes in the next build; ask them to test and report back. Follow the project's `AGENTS.md` on posting and closing.

## Deliver

The text in one fenced block ready to paste (a title in its own block), then one line naming anything uncertain or unverified. Post it yourself only when the user or the project's rules say so.
