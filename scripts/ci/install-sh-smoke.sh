#!/bin/bash
# install-sh-smoke.sh — sandboxed deploy smoke test for install.sh (gate 3).
#
# Runs the real install.sh against a fake $HOME and a shimmed `omarchy`
# CLI, pinning the round-1/round-2 deploy-ordering guarantees:
#   validate staging  →  stop shell  →  swap  →  validate DEST  →  restart,
# with rollback of the previous tree on any post-swap failure and the shell
# never left down. Dependency-free: bash + coreutils + the same tools
# install.sh itself needs (rsync OR cp, jq, pgrep) — no quickshell.
#
# Scenarios (each in a fresh sandbox):
#   1  happy path                  — order validate staging/DEST + restart,
#                                    old dot-dirs cleaned up
#   2  staging validation fails    — exit non-zero, DEST untouched, no
#                                    restart entry, shell never stopped
#   3  post-swap validation fails  — exit non-zero, old tree restored,
#                                    rollback restart attempted
#   4  restart fails               — exit non-zero, old tree restored,
#                                    exactly two restart attempts
#
# On a LIVE Omarchy machine the real quickshell answers to the pattern that
# install.sh pkills, so the guard below refuses to run; use a PID namespace
# (the sandboxed install.sh then cannot even see the real shell):
#   unshare --user --pid --fork --mount-proc bash scripts/ci/install-sh-smoke.sh
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
PLUGIN_ID="io.github.glasschan.appimage"
QS_PAT='quickshell -n -p /usr/share/omarchy/shell'

die() {
  printf 'install-sh-smoke: FAIL: %s\n' "$*" >&2
  if [[ -n "${SBX:-}" && -f "$SBX/install.out" ]]; then
    echo '--- install.sh output (tail) ---' >&2
    tail -n 20 -- "$SBX/install.out" >&2
    echo '--- shim log ---' >&2
    cat -- "$LOG" >&2
  fi
  exit 1
}

# Safety: install.sh (sandboxed below) pkills this exact pattern. Refuse to
# run where a real quickshell answers to it; only our own stale fakes from
# an earlier crashed run (a sleep with a rewritten argv[0]) are cleaned up.
for pid in $(pgrep -f -- "$QS_PAT" || true); do
  exe="$(readlink "/proc/$pid/exe" 2>/dev/null || true)"
  case "$exe" in
    */sleep|*/bash)
      kill "$pid" 2>/dev/null || true ;;
    *)
      die "pid $pid ($exe) matches the quickshell pattern — a real Omarchy shell is running; refusing to pkill it. Run this smoke test inside a PID namespace: unshare --user --pid --fork --mount-proc bash scripts/ci/install-sh-smoke.sh" ;;
  esac
done

for tool in jq pgrep pkill find; do
  command -v "$tool" >/dev/null 2>&1 ||
    die "required tool '$tool' not found"
done

WORK="$(mktemp -d)"
SBX="" LOG="" DEST="" FAKE_PID="" MARKER=""
cleanup() {
  # The fake shell is usually already dead (install.sh pkill'd it); never
  # let cleanup failures flip the script's exit status.
  if [[ -n "$FAKE_PID" ]]; then
    kill "$FAKE_PID" 2>/dev/null || true
  fi
  if [[ -n "$MARKER" ]]; then
    rm -f -- "$MARKER"
  fi
  rm -rf -- "$WORK"
}
trap cleanup EXIT

# --- Sandbox pieces. ---------------------------------------------------------
make_shim() {
  mkdir -p -- "$SBX/shim"
  cat > "$SBX/shim/omarchy" <<'SHIM'
#!/bin/bash
# Test shim for the omarchy CLI (scripts/ci/install-sh-smoke.sh).
# Steered failures, all env-controlled:
#   GATE_FAIL_DEST=1     'plugin validate <DEST>' exits 1
#   GATE_FAIL_RESTART=1  'restart shell' exits 1
#   a .gate-fail marker file anywhere in a validated tree fails it
set -u
LOG="${GATE_LOG:?GATE_LOG must be set}"
printf '%s\n' "$*" >> "$LOG"
cmd="${1:-}"; sub="${2:-}"; target="${3:-}"
case "$cmd $sub" in
  'plugin validate')
    if [[ -n "${GATE_FAIL_DEST:-}" && "$target" == "${GATE_DEST:-}" ]]; then
      exit 1
    fi
    if [[ -n "$target" ]] &&
      find "$target" -name '.gate-fail' -print -quit 2>/dev/null | grep -q .; then
      exit 1
    fi
    exit 0 ;;
  'restart shell')
    [[ -n "${GATE_FAIL_RESTART:-}" ]] && exit 1
    exit 0 ;;
  'plugin list')
    printf '[]\n'
    exit 0 ;;
  *)
    echo "omarchy-shim: unexpected call: $*" >&2
    exit 1 ;;
esac
SHIM
  chmod +x "$SBX/shim/omarchy"
}

# Fake running shell: argv[0] renamed so install.sh's stop block
# (pgrep/pkill -f on the quickshell pattern) actually fires. Lives in a
# script file so the pattern never appears in any process command line.
make_fake_shell() {
  cat > "$SBX/fake-shell" <<FAKE
#!/bin/bash
exec -a '$QS_PAT' sleep 30
FAKE
  chmod +x "$SBX/fake-shell"
}

new_sandbox() {
  SBX="$WORK/$1"
  mkdir -p -- "$SBX/home"
  LOG="$SBX/shim.log"
  : > "$LOG"
  DEST="$SBX/home/.config/omarchy/plugins/$PLUGIN_ID"
  make_shim
  make_fake_shell
}

# A previous installation with a canary file, so swap/rollback behaviour is
# observable (the tree staged from the repo never contains canary.txt).
seed_old_tree() {
  mkdir -p -- "$DEST/icons"
  printf 'manifest-old\n' > "$DEST/manifest.json"
  printf 'canary-old-v1\n' > "$DEST/canary.txt"
  printf 'old-icon\n' > "$DEST/icons/old.png"
}

spawn_fake_shell() {
  "$SBX/fake-shell" &
  FAKE_PID=$!
}

shell_alive() { kill -0 "$FAKE_PID" 2>/dev/null; }

run_install() {
  local rc=0
  (
    export HOME="$SBX/home"
    export XDG_CONFIG_HOME="$HOME/.config"
    export PATH="$SBX/shim:$PATH"
    export GATE_LOG="$LOG" GATE_DEST="$DEST"
    export GATE_FAIL_DEST="${GATE_FAIL_DEST:-}" GATE_FAIL_RESTART="${GATE_FAIL_RESTART:-}"
    "$REPO_ROOT/install.sh"
  ) > "$SBX/install.out" 2>&1 || rc=$?
  INSTALL_RC=$rc
}

dot_dirs_left() {
  find "$(dirname -- "$DEST")" -mindepth 1 -maxdepth 1 -name '.*' | wc -l
}

# Validate/validate/restart event sequence, in order (other shim calls,
# e.g. 'plugin list --json', are filtered out). The staging line is anchored
# on install.sh's dot-prefixed .staging.<pid> dir name (a plain 'staging'
# substring would also match arbitrary sandbox paths).
assert_event_order() {
  local events
  events="$(grep -E '^(plugin validate|restart shell)' -- "$LOG")"
  [[ "$(grep -c '^plugin validate .*\.staging\.' <<<"$events" || true)" == "1" ]] ||
    die "$1: expected exactly one staging validation, log:
$events"
  [[ "$(grep -c '^plugin validate' <<<"$events" || true)" == "2" ]] ||
    die "$1: expected exactly two validations (staging then DEST), log:
$events"
  sed -n 2p <<<"$events" | grep -Fq -- "$DEST" ||
    die "$1: second validation is not of DEST, log:
$events"
  sed -n 3p <<<"$events" | grep -q '^restart shell$' ||
    die "$1: restart does not follow the DEST validation, log:
$events"
}

restart_count() { grep -c '^restart shell$' -- "$LOG" || true; }

# --- Scenarios. --------------------------------------------------------------
GATE_FAIL_DEST=""
GATE_FAIL_RESTART=""

# Scenario 1: happy path.
new_sandbox happy
seed_old_tree
spawn_fake_shell
run_install
[[ "$INSTALL_RC" == "0" ]] ||
  die "scenario 1 (happy path): install.sh exited $INSTALL_RC, expected 0"
assert_event_order "scenario 1"
[[ "$(restart_count)" == "1" ]] ||
  die "scenario 1: expected exactly one restart, log:
$(cat -- "$LOG")"
[[ -f "$DEST/manifest.json" ]] ||
  die "scenario 1: manifest.json missing from DEST after install"
cmp -s "$DEST/manifest.json" "$REPO_ROOT/manifest.json" ||
  die "scenario 1: DEST manifest.json is not the repo copy (old tree not replaced?)"
[[ ! -e "$DEST/canary.txt" ]] ||
  die "scenario 1: old canary.txt still present — old tree was not swapped out"
[[ "$(dot_dirs_left)" == "0" ]] ||
  die "scenario 1: leftover staging/.old dot-dirs: $(dot_dirs_left)"
! shell_alive ||
  die "scenario 1: fake shell still alive — install.sh never stopped it"
printf 'PASS: scenario 1 — happy path: validate staging, validate DEST, restart; old tree swapped out; dot-dirs cleaned\n'

# Scenario 2: staging validation fails — nothing is touched, shell keeps running.
new_sandbox pre-swap-gate
seed_old_tree
spawn_fake_shell
MARKER="$REPO_ROOT/icons/.gate-fail"
: > "$MARKER"
run_install
rm -f -- "$MARKER"
MARKER=""
[[ "$INSTALL_RC" != "0" ]] ||
  die "scenario 2 (staging validation fails): install.sh exited 0, expected failure"
[[ "$(grep -c '^plugin validate .*\.staging\.' -- "$LOG" || true)" == "1" ]] ||
  die "scenario 2: staging was never validated, log:
$(cat -- "$LOG")"
[[ "$(restart_count)" == "0" ]] ||
  die "scenario 2: restart attempted although staging failed validation, log:
$(cat -- "$LOG")"
[[ "$(cat -- "$DEST/canary.txt")" == "canary-old-v1" ]] ||
  die "scenario 2: DEST was modified although staging failed validation"
[[ "$(dot_dirs_left)" == "0" ]] ||
  die "scenario 2: leftover staging dot-dirs after abort: $(dot_dirs_left)"
shell_alive ||
  die "scenario 2: fake shell is dead — the shell must not be stopped before staging validates"
printf 'PASS: scenario 2 — staging validation failure: exit non-zero, DEST untouched, shell never stopped\n'

# Scenario 3: post-swap DEST validation fails — old tree restored + rollback restart.
GATE_FAIL_DEST=1
new_sandbox gate-fail-dest
seed_old_tree
spawn_fake_shell
run_install
GATE_FAIL_DEST=""
[[ "$INSTALL_RC" != "0" ]] ||
  die "scenario 3 (post-swap validation fails): install.sh exited 0, expected failure"
assert_event_order "scenario 3"
[[ "$(restart_count)" == "1" ]] ||
  die "scenario 3: expected exactly one (rollback) restart attempt, log:
$(cat -- "$LOG")"
[[ "$(cat -- "$DEST/canary.txt")" == "canary-old-v1" ]] ||
  die "scenario 3: old tree not restored after failed DEST validation"
[[ "$(dot_dirs_left)" == "0" ]] ||
  die "scenario 3: leftover dot-dirs after rollback: $(dot_dirs_left)"
printf 'PASS: scenario 3 — post-swap validation failure: old tree restored at DEST, rollback restart attempted\n'

# Scenario 4: restart fails — old tree restored after two restart attempts.
GATE_FAIL_RESTART=1
new_sandbox restart-fail
seed_old_tree
spawn_fake_shell
run_install
GATE_FAIL_RESTART=""
[[ "$INSTALL_RC" != "0" ]] ||
  die "scenario 4 (restart fails): install.sh exited 0, expected failure"
assert_event_order "scenario 4"
[[ "$(restart_count)" == "2" ]] ||
  die "scenario 4: expected exactly two restart entries (failed attempt + rollback attempt), log:
$(cat -- "$LOG")"
[[ "$(cat -- "$DEST/canary.txt")" == "canary-old-v1" ]] ||
  die "scenario 4: old tree not restored after failed restart"
[[ "$(dot_dirs_left)" == "0" ]] ||
  die "scenario 4: leftover dot-dirs after rollback: $(dot_dirs_left)"
printf 'PASS: scenario 4 — restart failure: old tree restored, exactly two restart attempts\n'

printf 'install-sh-smoke: PASS (4 scenarios)\n'
