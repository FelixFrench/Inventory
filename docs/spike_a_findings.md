# Spike A Findings — WebSocket End-to-End Viability

> Complete this document after LAN testing on the Raspberry Pi.

---

## 1. LAN Connectivity

Did `ws://` connect successfully from mobile and desktop browsers over LAN?
Any unexpected issues?

_To be completed after testing._

---

## 2. CSP

What change was required? Paste the amended `connect-src` value.

_To be completed after testing._

---

## 3. Reconnection

Did automatic reconnect work after server restart (`systemctl restart inventory`)?
How long did it take?

_To be completed after testing._

---

## 4. Multiple Clients

Did both clients receive the `/spike/broadcast` message? Did disconnecting one
client cause any issues for the server or the remaining client?

_To be completed after testing._

---

## 5. API Key / Auth

Confirm the decision: no authentication on `/ws`, LAN-only deployment accepted.
Note whether the existing middleware needed to be amended.

_To be completed after testing._

---

## 6. Anything Unexpected

Any issues not covered above that Phase 2 needs to know about.

_To be completed after testing._
