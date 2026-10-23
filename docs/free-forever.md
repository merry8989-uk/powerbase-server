# Free forever

Powerbase is free and open source, permanently. There is no paid tier, no
billing, no usage paywall, and no subscription checkout. Not now, and not
planned.

This is a commitment with a test attached, because a promise in a README is
worth exactly what the reader believes it is worth.

## How it is enforced

`tests/test_free_forever.py` fails the build if any of this stops being true:

| Test | What it refuses |
|---|---|
| `test_no_monetisation_vocabulary_exists` | Scans every source, SQL, config and manifest file for the vocabulary of billing — payment processors, `billing`, `paywall`, `plan_tier`, `seat_count`, `trial_expired`. An ambiguous word like `checkout` or `invoice` is flagged only when a payment processor is on the same line, because `checkout` is also the name of this project's sample e-commerce app. |
| `test_no_permission_is_plan_shaped` | The permission table is the only way any route can be refused. If no permission is about entitlement, no feature can be withheld from someone who has not paid — because there is nothing to pay. |
| `test_every_role_is_reachable_without_payment` | All four roles are access levels, not tiers. The highest, `owner`, is simply the first account created by whoever runs the server. |
| `test_no_route_is_gated_on_a_plan` | Every permission a route names must exist in the table, and the table is clean. |
| `test_the_licence_is_present_and_osi_approved` | A recognised OSI licence exists. (This one found that the file was missing.) |
| `test_nothing_sends_data_anywhere_by_default` | A fresh install has no alert channels, no drains, no webhook and no SMTP configured. The server makes no outbound call unless the operator made one. |

The test's own helper asserts it is really scanning the tree — a scan that
silently finds nothing would prove nothing.

## What "free" does not mean

It does not mean every deployment is somebody else's to pay for. Powerbase runs
on hardware the operator provides, and that costs what it costs.

It does not mean unlimited. There are rate limits, retention limits, batch size
caps and a row cap on exports. Those are abuse prevention — they protect an
instance from being taken down by one client — and they are configurable by the
operator, not by a price list.

It does not mean no dependencies. The server needs Python and a few libraries;
the SDK has zero runtime dependencies; the app is built from a public repository.

## If you want to support it

Optional donations or sponsorship are fine, and the plans allow them. The one
rule is that they stay outside the product: no feature may depend on one, and
nothing in the code may check whether one happened.
