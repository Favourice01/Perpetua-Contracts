# Factory Specification

`FluxoraFactory` is an optional policy gate around `FluxoraStream`. Each
contract invocation reads one ledger snapshot; callers must still handle stale
RPC simulations by refreshing the ledger state and retrying the transaction.
The factory cannot detect an RPC node's read skew.

## Initialization and Stored Policy

`init(admin, stream_contract, stream_wasm_hash, max_deposit, min_duration)` is
one-time. It rejects a non-positive deposit cap and a minimum duration above
the default maximum. The initial policy has creation unpaused, recipient
allowlist enforcement disabled, rate bounds unset, and no active-stream cap.
The reviewed WASM hash is stored as configuration metadata; the factory does
not deploy or independently verify the configured stream contract.
Changes to this interface or stored configuration require a new factory
deployment; the existing deployment has no upgrade or in-place migration path.

The instance configuration is extended on every load and policy update using a
17,280-ledger threshold and a 120,960-ledger bump. Allowlist and rate-limit
records use persistent storage and receive the same TTL treatment when read or
written. A missing or archived persistent key is treated as absent, not as a
host error: an archived allowlist entry denies a restricted recipient, while
an archived rate bucket starts a new window.

## Creation Path

`create_stream` checks policy before invoking the configured stream contract:

1. Reject when globally paused.
2. If recipient allowlisting is enabled, require a live allowlist entry.
3. Check the active-stream limit and per-stream deposit cap.
4. Require a positive deposit, a non-zero duration within the configured
   inclusive bounds, a cliff within `[start_time, end_time]`, and any configured
   per-second rate bounds.
5. If the sender rate limit is enabled, reserve a creation in its ledger window.
6. Invoke `create_stream`; increment the active count only after success.

Soroban transaction atomicity is relied on deliberately. If the downstream
stream invocation fails, the rate-limit reservation and all factory writes
from that transaction roll back, so retrying does not consume quota or active
capacity. A batch creation endpoint is not exposed by this factory; governance
batch dispatch is atomic at the transaction level.

`set_allowlist(recipient, true)` enables recipient enforcement and records the
entry. `set_allowlist_enabled` can explicitly turn enforcement on or off
without deleting entries. `set_token_allowed` and `validate_token` maintain and
query a separate token allowlist; `create_stream` does not implicitly call
`validate_token`.

The terminal callback authorizes only the configured stream contract and
guards against decrementing an already-zero counter. The integration must call
it exactly once per terminal stream; the factory does not persist a per-stream
callback marker.

The optional sender quota is disabled by default (`u32::MAX` creations per
window). `set_rate_limit` requires both the maximum and window length to be
non-zero. Window reset uses saturating ledger subtraction so a lower sequence
number from a test or stale simulation cannot underflow the counter.

## Public Surface

| Entry point | Behavior |
|---|---|
| `init` | Initialize the admin, stream address/hash, cap, and minimum duration. |
| `get_factory_config` | Return the full configuration or `NotInitialized`. |
| `is_allowlisted`, `is_token_allowed` | Query persistent membership; successful reads refresh TTL. |
| `validate_token` | Return `TokenNotAllowed` when the token entry is absent. |
| `validate_duration` | Check inclusive duration bounds; zero and reversed ranges are invalid. |
| `record_creation` | Authenticated sender quota reservation. |
| `is_factory_paused`, `pending_admin` | Read pause and pending-admin state. |
| `derive_stream_salt` | Derive the domain-separated child salt from sender, nonce, and stream ID. |
| `set_admin`, `propose_admin`, `accept_admin` | Single-step or two-step admin rotation. |
| `set_stream_contract`, `set_stream_wasm_hash` | Update stream target metadata. |
| `set_cap`, `set_min_duration`, `set_max_duration` | Update creation bounds. |
| `set_allowlist`, `set_allowlist_enabled`, `set_token_allowed` | Update allowlist data and recipient enforcement. |
| `set_batch_cap_enforcement`, `set_rate_bounds`, `set_rate_limit`, `set_max_active_streams`, `set_pause` | Update optional policy controls. |
| `set_factory_paused` | Backward-compatible alias for `set_pause`. |
| `create_stream` | Validate policy, invoke the stream, then account for active capacity. |
| `stream_terminated` | Authenticated callback from the configured stream contract to release one active slot. |

Admin setters require the current admin's Soroban authorization. Stream creation
uses the stream contract's authorization and validation as the final authority
for stream-specific constraints not duplicated by the factory.

## Factory Errors

| Code | Error | Meaning |
|---:|---|---|
| 1 | `NotInitialized` | Configuration is absent or archived. |
| 2 | `AlreadyInitialized` | `init` has already succeeded. |
| 3 | `Unauthorized` | Reserved authorization error; current setters use Soroban `require_auth`. |
| 4 | `AllowlistDenied` | Recipient is not present while enforcement is enabled. |
| 5 | `InvalidNewAdmin` | New admin is the zero address or this contract. |
| 6 | `NoPendingAdmin` | No successor was nominated. |
| 7 | `InvalidDuration` | Duration is reversed, zero, or outside configured bounds. |
| 8 | `TokenNotAllowed` | Token is absent from the token allowlist. |
| 9 | `RateLimitExceeded` | Sender has used the configured window quota. |
| 10 | `InvalidRateLimit` | Quota or window length is zero. |
| 11 | `CapacityCapExceeded` | Active-stream capacity is full. |
| 12 | `ActiveStreamCountUnderflow` | A termination callback would decrement zero active streams. |
| 13 | `CreationPaused` | New stream creation is paused. |
| 14 | `DepositCapExceeded` | Deposit exceeds the configured maximum. |
| 15 | `InvalidRateBounds` | A rate bound is negative or minimum exceeds maximum. |
| 16 | `RateBelowMinimum` | Computed per-second deposit rate is below the configured minimum. |
| 17 | `RateAboveMaximum` | Computed per-second deposit rate exceeds the configured maximum. |
| 18 | `InvalidDeposit` | Deposit or configured cap is non-positive. |
| 19 | `InvalidCliff` | Cliff is outside the stream's inclusive time range. |

Cross-contract errors from `FluxoraStream` are propagated by the host. A failed
downstream call reverts the complete transaction, including factory
reservations and counters.