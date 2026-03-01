# State Machines

Formal state descriptions for the SSO watcher system. Designed for both human review and LLM/agent consumption.

## Watcher Modes

Four modes, switchable at runtime without restart via `mise run sso-mode:*`.

```mermaid
stateDiagram-v2
    direction LR
    [*] --> notify : default
    notify --> auto : sso-mode auto
    notify --> silent : sso-mode silent
    notify --> standalone : sso-mode standalone
    auto --> notify : sso-mode notify
    auto --> silent : sso-mode silent
    auto --> standalone : sso-mode standalone
    silent --> notify : sso-mode notify
    silent --> auto : sso-mode auto
    silent --> standalone : sso-mode standalone
    standalone --> notify : sso-mode notify
    standalone --> auto : sso-mode auto
    standalone --> silent : sso-mode silent
```

## Notify Mode Flow

Default mode. Tries silent refresh first, then shows macOS dialog.

```mermaid
stateDiagram-v2
    direction TB
    [*] --> polling

    polling --> silent_refresh : signal + no cooldown + no snooze
    silent_refresh --> polling : success, clears signal + writes cooldown

    silent_refresh --> dialog : failed

    state dialog {
        direction LR
        [*] --> waiting
        waiting --> refresh : clicks Refresh
        waiting --> snooze : clicks Snooze
        waiting --> suppress : clicks Dont Remind
        waiting --> dismiss : closes or 120s timeout
    }

    dialog --> login : refresh
    dialog --> polling : snooze, writes nextAttemptAfter
    dialog --> polling : suppress, clears signal
    dialog --> polling : dismiss, writes cooldown

    login --> polling : exit 0, clears signal + writes cooldown
    login --> polling : nonzero or timeout, writes 30s snooze
```

## Auto Mode Flow

Tries silent refresh first, then opens browser immediately. No dialog.

```mermaid
stateDiagram-v2
    direction LR
    [*] --> polling
    polling --> silent_refresh : signal + no cooldown + no snooze
    silent_refresh --> polling : success, clears signal + writes cooldown
    silent_refresh --> login : failed
    login --> polling : exit 0, clears signal + writes cooldown
    login --> polling : nonzero or timeout, writes 30s snooze
```

## Silent Mode Flow

Tries silent token refresh only. Never opens browser.

```mermaid
stateDiagram-v2
    direction LR
    [*] --> polling
    polling --> silent_refresh : signal + no cooldown + no snooze
    silent_refresh --> polling : success, clears signal + writes cooldown
    silent_refresh --> polling : failed, writes 30s snooze
```

## Standalone Mode

Watcher idles. User runs `mise run sso-login` manually.

```mermaid
stateDiagram-v2
    direction LR
    [*] --> idle
    idle --> idle : polls every 5s, ignores signals
```

## Proactive Refresh (independent of signal)

```mermaid
stateDiagram-v2
    direction LR
    [*] --> checking : every 60s
    checking --> healthy : token valid, >30min left
    checking --> refreshing : token near expiry, <30min left
    refreshing --> healthy : silent refresh success
    refreshing --> waiting : silent refresh failed
    healthy --> checking : 60s
    waiting --> checking : monitor will signal if expired
```

## Signal Lifecycle

The signal file (`~/.aws/sso-renewer/login-required.json`) drives the watcher.

```mermaid
stateDiagram-v2
    direction TB
    [*] --> no_signal

    no_signal --> created : monitor detects expiry
    no_signal --> created : manual sso-login

    created --> handling : watcher picks up signal
    created --> snoozed : user snoozes

    snoozed --> handling : snooze expired

    handling --> no_signal : success or suppress
    handling --> snoozed : failed, 30s auto-snooze
    handling --> created : dismissed, cooldown written
```

## Cooldown vs Snooze

Two separate throttle mechanisms.

**Cooldown** — file-based (`last-login-at.txt`), blocks ALL signal processing:

```mermaid
stateDiagram-v2
    direction LR
    [*] --> ready
    ready --> throttled : dismiss or suppress or success
    throttled --> ready : 600s elapsed
    throttled --> ready : sso-login or sso-logout clears file
```

**Snooze** — in-signal (`nextAttemptAfter`), blocks only the current signal:

```mermaid
stateDiagram-v2
    direction LR
    [*] --> ready
    ready --> snoozed : user picks 15m to 4h
    ready --> snoozed : login failed, auto 30s
    snoozed --> ready : timestamp reached
```

## State File Summary

| File | Written by | Read by | Purpose |
|------|-----------|---------|---------|
| `login-required.json` | monitor, sso-login | watcher | Trigger: credentials expired |
| `last-login-at.txt` | watcher on dismiss/suppress/success | watcher, sso-status | Cooldown: prevent spam |
| `mode` | sso-mode:* | watcher, sso-status, sso-login, sso-logout | Runtime mode override |
| `login.lock/` | watcher via mkdir | watcher | Concurrency: single login |

All files in `~/.aws/sso-renewer/`.

## Transition Table (for agents)

Machine-readable transition table for the main watcher loop:

```
STATE                | CONDITION                          | ACTION                        | NEXT
---------------------|------------------------------------|------------------------------ |------------------
polling              | no signal file                     | sleep                         | polling
polling              | signal + cooldown active           | sleep                         | polling
polling              | signal + snooze active             | sleep                         | polling
polling              | signal + lock held                 | sleep                         | polling
polling              | signal + ready + lock acquired     | handle_login                  | handling
handling (any)       | silent refresh succeeds            | clear signal, write cooldown  | polling
handling (notify)    | silent fail > dialog > refresh > 0 | clear signal, write cooldown  | polling
handling (notify)    | silent fail > dialog > refresh > !0| write 30s snooze to signal    | polling
handling (notify)    | silent fail > dialog > snooze      | write snooze to signal        | polling
handling (notify)    | silent fail > dialog > suppress    | clear signal, write cooldown  | polling
handling (notify)    | silent fail > dialog > dismiss     | write cooldown                | polling
handling (auto)      | silent fail > login > exit 0       | clear signal, write cooldown  | polling
handling (auto)      | silent fail > login > nonzero      | write 30s snooze to signal    | polling
handling (silent)    | silent fail                        | write 30s snooze to signal    | polling
standalone           | any                                | sleep                         | standalone
```
