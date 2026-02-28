# State Machines

Formal state descriptions for the SSO watcher system. Designed for both human review and LLM/agent consumption.

## Watcher Mode State Machine

The watcher operates in one of three modes, switchable at runtime without restart.

```mermaid
stateDiagram-v2
    [*] --> notify : default, no mode file

    notify --> auto : sso-mode auto
    notify --> standalone : sso-mode standalone
    auto --> notify : sso-mode notify
    auto --> standalone : sso-mode standalone
    standalone --> notify : sso-mode notify
    standalone --> auto : sso-mode auto

    state notify {
        [*] --> polling_notify
        polling_notify --> dialog_shown : signal exists + no cooldown + no snooze
        dialog_shown --> login_running : user clicks Refresh
        dialog_shown --> snoozed : user clicks Snooze
        dialog_shown --> suppressed : user clicks Dont Remind
        dialog_shown --> dismissed : user closes / 120s timeout
        login_running --> success : exit code 0
        login_running --> failed : nonzero exit or 120s timeout
        success --> polling_notify : signal cleared, cooldown written
        failed --> polling_notify : 30s snooze written to signal
        snoozed --> polling_notify : nextAttemptAfter written to signal
        suppressed --> polling_notify : signal cleared, cooldown written
        dismissed --> polling_notify : cooldown written, signal kept
    }

    state auto {
        [*] --> polling_auto
        polling_auto --> auto_login : signal exists + no cooldown + no snooze
        auto_login --> auto_success : exit code 0
        auto_login --> auto_failed : nonzero exit or 120s timeout
        auto_success --> polling_auto : signal cleared, cooldown written
        auto_failed --> polling_auto : 30s snooze written to signal
    }

    state standalone {
        [*] --> idle
        idle --> idle : polls every 5s, ignores signals
        note right of idle
            User runs sso-login manually
        end note
    }
```

## Signal Lifecycle

The signal file (`~/.aws/sso-renewer/login-required.json`) drives the watcher.

```mermaid
stateDiagram-v2
    [*] --> no_signal : credentials valid

    no_signal --> signal_created : monitor detects expiry
    no_signal --> signal_created : sso-login in notify or auto mode
    no_signal --> no_signal : sso-login in standalone mode

    signal_created --> signal_created : watcher polls, cooldown active
    signal_created --> signal_snoozed : user snoozes
    signal_created --> handling : watcher picks up signal

    signal_snoozed --> signal_snoozed : snooze not expired yet
    signal_snoozed --> handling : snooze expired

    handling --> no_signal : login success, signal cleared
    handling --> no_signal : user suppresses, signal cleared
    handling --> signal_snoozed : login failed, 30s snooze
    handling --> signal_created : user dismisses, cooldown written
```

## Cooldown vs Snooze

Two different throttle mechanisms prevent dialog/login spam.

```mermaid
stateDiagram-v2
    state cooldown {
        [*] --> no_cooldown
        no_cooldown --> active : dismiss or suppress or success
        active --> no_cooldown : 600s elapsed
        active --> no_cooldown : sso-login or sso-logout clears file
        note right of active
            Blocks all signal processing
            Default 600s
        end note
    }

    state snooze {
        [*] --> no_snooze
        no_snooze --> snooze_active : user picks duration
        no_snooze --> snooze_active : login failed, auto 30s
        snooze_active --> no_snooze : epoch timestamp reached
        note right of snooze_active
            Written inside signal file
            Only blocks THIS signal
        end note
    }
```

## State File Summary

| File | Written by | Read by | Purpose |
|------|-----------|---------|---------|
| `login-required.json` | monitor container, `mise run sso-login` | watcher | Trigger: credentials expired |
| `last-login-at.txt` | watcher (on dismiss/suppress/success) | watcher, `sso-status` | Cooldown: prevent dialog spam |
| `mode` | `mise run sso-mode:*` | watcher (every poll), `sso-status`, `sso-login`, `sso-logout` | Runtime mode override |
| `login.lock/` | watcher (mkdir) | watcher | Concurrency: single login at a time |

All files live in `~/.aws/sso-renewer/`.

## Transition Table (for agents)

Machine-readable transition table for the main watcher loop (notify/auto modes):

```
CURRENT_STATE        | CONDITION                          | ACTION                    | NEXT_STATE
---------------------|------------------------------------|---------------------------|------------------
polling              | no signal file                     | sleep(poll)               | polling
polling              | signal + cooldown active           | sleep(poll)               | polling
polling              | signal + snooze active             | sleep(poll)               | polling
polling              | signal + lock held                 | sleep(poll)               | polling
polling              | signal + ready + lock acquired     | handle_login()            | handling
handling (notify)    | dialog → refresh → exit 0          | clear signal, write cooldown | polling
handling (notify)    | dialog → refresh → exit != 0       | write 30s snooze to signal | polling
handling (notify)    | dialog → snooze                    | write snooze to signal    | polling
handling (notify)    | dialog → suppress                  | clear signal, write cooldown | polling
handling (notify)    | dialog → dismiss/timeout           | write cooldown            | polling
handling (auto)      | login → exit 0                     | clear signal, write cooldown | polling
handling (auto)      | login → exit != 0                  | write 30s snooze to signal | polling
standalone           | any                                | sleep(poll)               | standalone
```
