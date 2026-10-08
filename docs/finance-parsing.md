# How Finance reads emails

How an email from your bank or from Amazon becomes a transaction or an order,
what happens when the regexes can't read it, and how the regex compiler writes
new regexes for you. Code: `apps/finance/` (`parsers.py`, `amazon.py`,
`learn.py`, `sync.py`, `orders.py`, `extract.py`).

## The short version

1. Every email is first tried against **regexes**. They are free, instant and
   private.
2. Every regex, built-in or learned, is a **row in the database** with a
   score. Rules are tried **best score first**; the first one that matches
   wins. There is no fixed order.
3. If no regex reads an email, it is written to the **miss log** and the
   **local model** reads it instead (if it is running).
4. From the misses, the **regex compiler** can ask the local model to write a
   new regex. It tests the regex on the real emails and, if it fails, tells
   the model why and asks again (up to 20 replies). A working regex waits for
   **your approval** before it is used.

The model is only ever a local one (LM Studio or Ollama), pinned by
`[apps.finance.ai]` in `hub.toml`. Emails are masked before they are sent:
digit runs of 8 or more become `XXXX1234`.

## The rules table

All regexes live in `learned_parsers`:

| Column | Meaning |
|---|---|
| `kind` | `bank` (HDFC alerts) or `amazon` (order emails) |
| `name` | Unique per kind, e.g. `upi_debit`. Learned ones show as `learned:<name>` on a transaction |
| `pattern` | The regex (case-insensitive). Amazon rules may also have `item_pattern` |
| `builtin` | `1` for the regexes that ship with the code. They are added to the table the first time they are needed |
| `status` | `active`, `proposed` (waiting for you), `disabled`, `rejected` |
| `matches` | **The score.** Goes up by 1 each time this regex reads an email |
| `last_matched_at` | When it last did |

Rules are loaded with `ORDER BY matches DESC, id`. The most useful regexes
get tried first, and ties keep the original order. **Finance → Parsers →
Scorecard** shows them all, with scores. Any rule can be disabled and
re-enabled there; built-in rules can't be edited or deleted.

What counts as a match (and scores): the regex produced the transaction or
the order data that was stored. These **don't** score: emails read by the
model, emails nobody could read, and the test runs the regex compiler does on
samples.

### Order must not matter

Because the order follows the scores, two regexes must never both match the
same email in different ways. The built-in patterns are written to be
mutually exclusive (for example `netbanking_v1` refuses "to VPA …", which
belongs to `upi_debit`; the loose Amazon total steps aside when a labelled
"Order total" exists). `tests/test_finance_learn.py` runs every fixture in
reversed and shuffled orders and fails if any result changes. **When you add
a built-in regex, keep it exclusive and extend that test.**

Learned regexes are not checked against each other. A broad one that builds
up a high score could win over a more specific one; the scorecard shows this,
and you can disable it.

## Bank alerts (HDFC)

For each stored email (`sync._process_email`):

1. The whitespace-collapsed body is tried against the active `bank` rules.
   Ten are built in: `upi_debit`, `upi_credit`, `card_spend_v1`,
   `card_spend_v2`, `atm_withdrawal_v1`, `atm_withdrawal_v2`, `card_credit`,
   `netbanking_v1`, `netbanking_v2`, `account_credit`.
   Every rule needs an `(?P<amount>…)` group. Optional groups are `date`,
   `acct` (last 4), `merchant` and `vpa`; direction and instrument are fixed
   per rule.
2. A match gives a transaction (amount in paise, direction, instrument,
   counterparty, reference, date), the rule's score goes up, and the email is
   `parsed`.
3. No match, but the subject or the first 400 characters look like an OTP,
   statement, due-date reminder, login notice and so on: the email is
   `ignored`. That is not a miss.
4. Otherwise it is a **miss**: the local model reads it (`extract.py`). If
   the model returns a transaction it is stored with `source = 'ai'`; if the
   model says it isn't one, the email is `ignored`; if the model is down or
   fails, the email stays `unparsed` on the **Review** page.

## Amazon orders

For each order email (`orders._process_email`, `amazon.parse_email`):

- **Order number** (`123-1234567-1234567`): fixed pattern in code. No order
  number and no "Ordered/Shipped/Delivered/Cancelled" subject means the email
  is ignored.
- **Total:** the active `amazon` total rules, best score first. Two are built
  in: `total_labelled` ("Order total / Grand total / Total amount / Order
  value") and `total_loose` ("Amount payable", a bare "Total"…). "Subtotal" is
  never taken as the total. A rule needs an `(?P<total>…)` group.
- **Items:** the line-by-line reader in code first (a title line followed by
  "Quantity: n" and a price). If it finds none, active rules with an
  `item_pattern` are tried (needs `(?P<title>…)`, optionally `qty`, `price`,
  matched on the body line by line). If that finds none, the item is taken
  from the subject line.
- **Status** comes from the subject and the start of the body.
- No total anywhere but every item has a single-quantity price? The total is
  estimated from the prices (shown as "estimated").

An order is **weak** if its items came only from the subject, or its total is
missing, and it is a fresh "placed" order. Weak orders and unreadable ones are
misses, and the local model fills in what the regexes couldn't (`ai_fill`).

## The miss log

Every miss is written to `parse_misses` with what happened next:

| Outcome | Meaning |
|---|---|
| `ai_parsed` | The model read it |
| `ai_ignored` | The model said it isn't a transaction or order |
| `ai_failed` | The model was tried and couldn't |
| `ai_skipped` | The model wasn't tried (off, down, or re-parse without it) |

A miss is removed as soon as a regex reads that email. **Finance → Parsers**
shows the counts and the most recent misses, and the log is also in the
database. Misses are also written to the app log (`regex miss (bank) …`).

## The regex compiler

On **Parsers**, **Ask the model for regexes** starts a background job per
source (`learn_bank`, `learn_amazon`). It can take minutes on a local model;
the page shows "Model reply 7 of 20" and reloads when done. It does nothing if
there are proposals still waiting for you.

**Samples:** the 10 newest misses, skipping `ai_ignored` ones, masked and
trimmed. For bank misses the model already read, its amount is kept as the
answer the new regex must agree with.

**The conversation** (`learn.negotiate`, one `ctx.ai.complete` call per reply,
max **20 model replies**):

1. First prompt: the samples. The model replies in two parts: its thinking
   inside `<analysis>…</analysis>`, then the answer as JSON inside
   `<json>…</json>`: up to 3 bank regexes (each with direction and instrument),
   or an Amazon `total_pattern` and/or `item_pattern`. A fenced ```` ```json ````
   block or bare JSON is also accepted. The analysis is kept and shown on
   the Parsers page.
2. Each regex is tested on the same samples. It passes if it is safe, has the
   required named group, and reads **at least 2** of the samples (or all of
   them if fewer than 2) with amounts that agree with the model's earlier
   reading.
   - Safety: at most 600 characters, no nested quantifiers like `(a+)+`
     (Python's `re` has no timeout). This is a guard, not a proof; it is one
     of the reasons nothing runs without your approval.
3. If one or more pass, the conversation **stops**. They are saved as
   `proposed`.
4. If none passes, the model is asked again, with exactly what was wrong:
   the regex isn't valid or is unsafe; a named group is missing; "matched 1 of
   5 emails", then for each of the first three emails it missed, the text
   around the amount and **where the pattern breaks** ("matches up to
   `…debited from account 4321` but then expects `via upi`, while the email
   continues with `to VPA …`"); what the groups captured on an email it did
   read; or "your amount was 49 but it is 349". An unreadable reply is
   reported as such and counts as a reply.
5. After 20 replies without a pass the run is **failed**. A red line appears
   on the **dashboard** ("The regex compiler gave up on HDFC alerts after 20
   model replies") with a link to the whole conversation and a Dismiss
   button. A later successful run dismisses older failures. If the model can't
   be reached the dashboard shows a yellow warning instead, which is not
   counted as giving up.

**Why each reply gets a fresh prompt.** A model shown its own failed answers
again and again starts copying them, and then returns the same answer every
time. So the history is not replayed as chat turns. Every prompt is
self-contained: the emails, the model's previous attempt, what went wrong with
it, and a short list (last three) of patterns that already failed.

**Breaking a loop.** Each answer is compared (ignoring its name) with all the
earlier failed ones. A repeat:

- raises the temperature for the next reply, from 0.2 by 0.25 for each repeat
  in a row, up to 0.9 (local servers otherwise answer the same prompt the
  same way);
- shrinks the next prompt to **one** email the pattern still fails on and
  tells the model its answer was identical and a materially different
  pattern is needed.

A new answer puts the temperature and the full prompt back. A repeat still
counts as one of the 20 replies. The Parsers page shows each reply's
temperature and a note when it was a repeat.

If the hub restarts mid-run, the run is marked interrupted.

The conversation is stored (`regex_negotiations`), with the sample emails
saved once, and shown under **Parsers → Last conversation with the model**.

### Approving

Nothing a model writes is used until you approve it. A proposal shows its
pattern, how many samples it read, and what it extracted from the first one.

- **Approve & re-read missed emails** makes the rule `active`, re-reads the
  unparsed and ignored emails (and half-read Amazon orders) **using only
  regexes, no model**, and clears the misses it now covers.
- **Reject** discards it. **Disable** stops an active rule from being used;
  transactions it already created are kept.

## Where things are

| What | Where |
|---|---|
| Rules, scores, proposals, misses, conversations | **Finance → Parsers** |
| Emails nothing could read | **Finance → Review** (bank), **Orders** (Amazon) |
| Re-run the parsers over old emails | "Re-parse" buttons on Review and Orders |
| Tables | `learned_parsers`, `parse_misses`, `regex_negotiations`, `emails`, `order_emails` |
| Built-in regexes | `parsers.BUILTIN_SPECS` (bank), `amazon.BUILTIN_TOTALS` (Amazon totals) |
| Tests | `tests/test_finance_parsers.py`, `test_finance_amazon*.py`, `test_finance_learn.py`, fixtures in `tests/fixtures/` |

Adding or changing a built-in regex: edit the spec, keep it exclusive of the
others, add a fixture, and extend the order-independence test. New built-ins
are added to the table automatically the next time rules are loaded; edits to
an existing built-in's pattern are **not** (rows are only inserted, never
updated), so changing one needs a migration that updates that row.
