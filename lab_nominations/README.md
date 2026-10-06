# Lab nominations

One JSON packet per nominated spec, written by `python -m quant_system lab nominate`
and added here by PR. Each packet freezes the discovery trial count used for the
deflated Sharpe. The validation machine reads packets from here for `lab validate`
and `lab holdout`.

Don't edit a packet by hand. Its spec must still match `spec_hash`, and the frozen
count is checked against earlier nominations. Rules: section 11 of
[`docs/harness-upgrade-design.md`](../docs/harness-upgrade-design.md).

Validation and holdout results are never committed here.
