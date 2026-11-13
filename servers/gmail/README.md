# gmail server (planned, Week 3)

Phase 1 tools: `list_new_messages`, `get_message` (token scope gmail.readonly).
Phase 2 adds `propose_reply` → `apply_change` and `apply_label` behind the approval gate.
Every message body is untrusted input: tool output marks it as data, never instructions.
