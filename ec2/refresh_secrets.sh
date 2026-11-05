#!/usr/bin/env bash
# refresh_secrets.sh: pull the AWSCURRENT secret, write /etc/demo-app/secrets.env,
# restart svc@a/b/c, and verify each service loaded the new value via /health.
#
# - Uses the instance role (no keys on disk).
# - Never prints secret values: only the version id and 8-char sha256 prefixes.
# - Idempotent: same secret in => same file, same restart, same result.
# - Exit 0 = all services healthy with the expected hash; non-zero otherwise.
set -euo pipefail
umask 077

CONF=/etc/demo-app/refresh.conf
ENV_DIR=/etc/demo-app
ENV_FILE="$ENV_DIR/secrets.env"
LOCK=/run/refresh_secrets.lock
SERVICES=(a b c)
declare -A PORTS=([a]=8081 [b]=8082 [c]=8083)
HEALTH_WAIT_SECS=15    # per attempt, per service
MAX_RESTART_RETRIES=2  # extra per-service restarts if a service is unhealthy after the bulk restart

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) refresh_secrets: $*"; }
step() { echo; log "========== [$1/5] $2 =========="; }
run() { log "+ $*"; "$@"; } # show the exact command, then run it
die() { log "ERROR: $*"; echo "RESULT FAIL"; exit 1; }

[[ $EUID -eq 0 ]] || die "must run as root"
[[ -r $CONF ]] || die "missing $CONF"
# shellcheck source=/dev/null
source "$CONF"
[[ -n ${SECRET_ID:-} && -n ${AWS_REGION:-} ]] || die "SECRET_ID / AWS_REGION not set in $CONF"

# Optional $1: version id the caller (rotation Lambda) just promoted. If given, we
# refuse to load anything else (protects against eventually-consistent reads).
EXPECTED_VERSION=${1:-}
if [[ -n $EXPECTED_VERSION && ! $EXPECTED_VERSION =~ ^[A-Za-z0-9-]{1,64}$ ]]; then
  die "invalid expected version argument"
fi

# Serialize boot-time and SSM-triggered runs.
exec 9>"$LOCK"
flock -w 90 9 || die "timed out waiting for another refresh to finish"

log "started on $(hostname) as $(id -un) (pid $$, parent: $(ps -o comm= -p "$PPID" 2>/dev/null || echo '?'))"
log "secret=$SECRET_ID region=$AWS_REGION services=svc@{a,b,c} expected_version=${EXPECTED_VERSION:-<any: boot/manual run>}"

tmp_json=$(mktemp /run/refresh_secrets.XXXXXX) # tmpfs, mode 0600
tmp_env=""
cleanup() { rm -f "$tmp_json" ${tmp_env:+"$tmp_env"}; }
trap cleanup EXIT

# 1. Fetch AWSCURRENT (retry on API errors and, if an expected version was given,
#    until AWSCURRENT actually returns it). Only stderr is captured for logging.
step 1 "Fetch AWSCURRENT from Secrets Manager"
log "+ aws secretsmanager get-secret-value --secret-id $SECRET_ID --version-stage AWSCURRENT (value is never printed)"
FETCH_ATTEMPTS=6
fetched=0
last_problem=""
for ((attempt = 1; attempt <= FETCH_ATTEMPTS; attempt++)); do
  if err=$(aws secretsmanager get-secret-value \
    --region "$AWS_REGION" \
    --secret-id "$SECRET_ID" \
    --version-stage AWSCURRENT \
    --query '{v:VersionId,s:SecretString}' \
    --output json 2>&1 >"$tmp_json"); then
    got_version=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("v") or "")' "$tmp_json")
    if [[ -z $EXPECTED_VERSION || $got_version == "$EXPECTED_VERSION" ]]; then
      fetched=1
      break
    fi
    last_problem="AWSCURRENT returned $got_version, expected $EXPECTED_VERSION"
    log "WARN: attempt $attempt/$FETCH_ATTEMPTS: $last_problem (propagation delay), retrying"
  else
    last_problem=$(echo "$err" | tr '\n' ' ' | cut -c1-300)
    log "WARN: attempt $attempt/$FETCH_ATTEMPTS failed: $last_problem"
  fi
  if ((attempt < FETCH_ATTEMPTS)); then sleep 5; fi
done
((fetched)) || die "cannot read expected AWSCURRENT of '$SECRET_ID': $last_problem"

# 2. Validate JSON and render the env file into a temp file next to the target.
step 2 "Validate JSON (svc_a, svc_b, svc_c) and render env file"
tmp_env=$(mktemp "$ENV_DIR/.secrets.env.XXXXXX")
if ! summary=$(python3 - "$tmp_json" "$tmp_env" <<'PY'
import hashlib, json, re, sys

src, dst = sys.argv[1], sys.argv[2]
with open(src) as f:
    resp = json.load(f)
version = resp.get("v") or "unknown"
try:
    data = json.loads(resp.get("s") or "")
except ValueError:
    sys.exit("SecretString is not valid JSON")
if not isinstance(data, dict):
    sys.exit("SecretString is not a JSON object")

lines, out = [], [f"VERSION {version}"]
for svc in ("a", "b", "c"):
    key = f"svc_{svc}"
    val = data.get(key)
    if not isinstance(val, str) or not val:
        sys.exit(f"key '{key}' missing or empty")
    if re.search(r"[\x00-\x1f\x7f]", val):
        sys.exit(f"key '{key}' contains control characters")
    esc = val.replace("\\", "\\\\").replace('"', '\\"')
    lines.append(f'SVC_{svc.upper()}_SECRET="{esc}"')
    out.append(f"HASH {svc} {hashlib.sha256(val.encode()).hexdigest()[:8]}")

with open(dst, "w") as f:
    f.write("\n".join(lines) + "\n")
print("\n".join(out))
PY
); then
  die "secret payload failed validation (see message above)"
fi

version=$(awk '$1=="VERSION"{print $2}' <<<"$summary")
declare -A EXPECTED=()
while read -r tag svc hash; do
  if [[ $tag == HASH ]]; then EXPECTED[$svc]=$hash; fi
done <<<"$summary"
log "fetched AWSCURRENT version=$version"
for s in "${SERVICES[@]}"; do log "expected svc-$s hash=${EXPECTED[$s]}"; done

# 3. Install atomically: root:root 0600.
step 3 "Install $ENV_FILE atomically"
chown root:root "$tmp_env"
chmod 0600 "$tmp_env"
run mv -f "$tmp_env" "$ENV_FILE"
tmp_env=""
log "installed: $(stat -c '%U:%G %a %n' "$ENV_FILE")"

# 4. Restart services (keep going on error so we can report per-service health).
step 4 "Restart services"
units=()
for s in "${SERVICES[@]}"; do units+=("svc@$s.service"); done
declare -A PID_BEFORE=()
for u in "${units[@]}"; do PID_BEFORE[$u]=$(systemctl show -p MainPID --value "$u"); done
if ! run systemctl restart "${units[@]}"; then
  log "WARN: systemctl restart returned non-zero"
fi
for u in "${units[@]}"; do
  log "$u: pid ${PID_BEFORE[$u]} -> $(systemctl show -p MainPID --value "$u"), state=$(systemctl is-active "$u" || true), since $(systemctl show -p ActiveEnterTimestamp --value "$u")"
done

# 5. Health check: each service must report OK with the hash of the NEW value.
#    An unhealthy service is restarted individually (up to MAX_RESTART_RETRIES)
#    before it is declared FAIL.
got=""
check_health() { # $1 = service letter; sets global $got
  local port=${PORTS[$1]} want=${EXPECTED[$1]} i
  got=""
  for ((i = 0; i < HEALTH_WAIT_SECS; i++)); do
    got=$(curl -sS --max-time 2 "http://127.0.0.1:$port/health" 2>/dev/null || true)
    if [[ $got == "OK $want" ]]; then return 0; fi
    sleep 1
  done
  return 1
}

step 5 "Health check: /health must report the NEW hash (up to $MAX_RESTART_RETRIES restart retries per service)"
failed=0
for s in "${SERVICES[@]}"; do
  u="svc@$s.service"
  port=${PORTS[$s]}
  want=${EXPECTED[$s]}
  retries=0
  until check_health "$s"; do
    if ((retries >= MAX_RESTART_RETRIES)); then break; fi
    retries=$((retries + 1))
    log "WARN: svc-$s unhealthy (expected 'OK $want', got '${got:-no response}'); restart retry $retries/$MAX_RESTART_RETRIES"
    journalctl -u "$u" -n 5 --no-pager -o cat 2>/dev/null | sed "s/^/    journal $u: /" || true
    if ! run systemctl restart "$u"; then log "WARN: systemctl restart $u returned non-zero"; fi
    log "$u: pid now $(systemctl show -p MainPID --value "$u"), state=$(systemctl is-active "$u" || true)"
  done
  if [[ $got == "OK $want" ]]; then
    echo "PASS svc-$s port=$port hash=$want restart_retries=$retries"
  else
    echo "FAIL svc-$s port=$port expected=$want got='${got:-no response}' restart_retries=$retries"
    failed=1
  fi
done

echo
if ((failed)); then
  log "ERROR: one or more services failed health check (version=$version)"
  echo "RESULT FAIL version=$version"
  exit 2
fi
log "all services healthy (version=$version)"
echo "RESULT OK version=$version"
