# approval gate (planned, Week 3)

1. A `propose_*` tool changes nothing. It stores the proposed change in SQLite at
   `/opt/data/phoebe-state/approvals.sqlite` with a proposal ID and returns text for
   Phoebe to post in #phoebe-admin-tasks.
2. Phoebe posts it and passes the Slack message timestamp back.
3. `apply_change(proposal_id)` calls Slack `reactions.get` itself and applies the change
   only if a ✅ came from a user ID on the approver list (kept on the NAS, not in this
   repo). Phoebe's own bot user is never on the list.
4. Proposals expire after 72 hours and can be applied once.
