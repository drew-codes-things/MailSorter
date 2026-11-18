#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SITE_ENV="${SITE_ENV:-$REPO/deploy/site.env}"
SITE_NAMES=(PROXMOX CT SITE_HOST)
SITE_VALUE_PATTERN='^[A-Za-z0-9][A-Za-z0-9._:-]*$'
CT_SCRIPT=/root/mail-sorter-deploy-ct.sh

usage() {
  cat <<'USAGE'
usage: deploy.sh backup | apply [commit] | check | undo | google

Runs from the laptop and reaches the container through ssh to the Proxmox host
and `pct`. The host, the container and the site name come from deploy/site.env
(copy deploy/site.env.example).

  backup          copy the Caddyfile, /opt/mail-sorter, /etc/mail-sorter, the unit and a
                  consistent copy of the database into /root/mail-sorter-before-<stamp>.tgz
                  (mode 600) in the container
  apply [commit]  install or update Mail Sorter from the commit (default HEAD): service user,
                  venv, /var/lib/mail-sorter, env file and token key (both kept if present),
                  systemd unit, the marked Caddy site block (added once, rewritten when PORT
                  changed), then check; keeps the last 3 old code copies
  check           service, health, sign-in refusal, the site through Caddy, file modes, errors
  undo            put back what the last apply changed: the earlier code (or stop and disable
                  the service after a first install) and only the mail-sorter block in the
                  Caddyfile; /var/lib/mail-sorter, the env file and the token key stay
  google          asks for the Google OAuth client id and secret here on the laptop (the
                  secret is not shown while typed), writes them into
                  /etc/mail-sorter/mail-sorter.env in the container and restarts the service

Caddy is only changed while holding /root/caddy-edit.lock in the container (made with
noclobber, shared with the other deploy scripts), after the Caddyfile has been left
alone for 5 minutes, and always after `caddy validate`.
USAGE
}

site_value() {
  sed -n "s/^$1=//p" "$SITE_ENV" | tail -n 1
}

load_site() {
  local name value
  if [ ! -f "$SITE_ENV" ]; then
    echo "refusing: $SITE_ENV is missing; copy deploy/site.env.example to deploy/site.env and fill it in"
    exit 1
  fi
  for name in "${SITE_NAMES[@]}"; do
    value="${!name:-$(site_value "$name")}"
    if [[ ! "$value" =~ $SITE_VALUE_PATTERN ]]; then
      echo "refusing: $name in $SITE_ENV must be a plain host name, address, number or user name, got '$value'"
      exit 1
    fi
    printf -v "$name" '%s' "$value"
  done
}

write_ct_script() {
  cat <<'CT_SIDE'
#!/usr/bin/env bash
set -euo pipefail
APP=/opt/mail-sorter
DATA=/var/lib/mail-sorter
CONF=/etc/mail-sorter
ENV_FILE="$CONF/mail-sorter.env"
TOKEN_KEY="$CONF/token.key"
UNIT=/etc/systemd/system/mail-sorter.service
CADDYFILE=/etc/caddy/Caddyfile
STATE=/root
CADDY_LOCK="$STATE/caddy-edit.lock"
BACKUP_POINTER="$STATE/mail-sorter-last-backup"
BEFORE_POINTER="$STATE/mail-sorter-code-before"
CADDY_CHANGE="$STATE/mail-sorter-caddy-change"
CADDY_BLOCK_BEFORE="$STATE/mail-sorter-caddy-block-before"
CADDY_BLOCK_BEGIN="# >>> mail-sorter block, added and removed by the mail-sorter deploy.sh"
CADDY_BLOCK_END="# <<< mail-sorter block"
CADDY_BLOCK_SOURCE="$APP/deploy/Caddyfile.mail-sorter"
SITE_HOST="${SITE_HOST:?SITE_HOST is missing: deploy.sh passes it from deploy/site.env}"
SERVICE=mail-sorter
SERVICE_USER=mail-sorter
KEPT_CODE_COPIES=3
KEPT_BACKUPS=3
CADDY_QUIET_SECONDS=300
CADDY_LOCK_WAIT_SECONDS=900
CLIENT_ID_PATTERN='^[A-Za-z0-9._-]{1,200}\.apps\.googleusercontent\.com$'
CLIENT_SECRET_PATTERN='^[A-Za-z0-9._~+/=-]{8,200}$'
PORT=""
PORT_SOURCE=""
LOCAL=""
HOLDING_CADDY_LOCK=""

port_in() {
  sed -n 's/^PORT=\([0-9]\{1,5\}\)[[:space:]]*$/\1/p' "$1" | tail -n 1
}

load_port() {
  local env_path="${1:-$ENV_FILE}"
  if [ ! -f "$env_path" ]; then
    echo "refusing: $env_path is missing, so the port is unknown"
    exit 1
  fi
  PORT="$(port_in "$env_path")"
  if [ -z "$PORT" ]; then
    echo "refusing: $env_path has no PORT=<number> line"
    exit 1
  fi
  LOCAL="http://127.0.0.1:$PORT"
}

code_of() {
  curl -s -o /dev/null -w '%{http_code}' "$@"
}

wait_for_health() {
  for _ in $(seq 1 30); do
    if [ "$(code_of "$LOCAL/api/health")" = 200 ]; then
      return 0
    fi
    sleep 1
  done
  echo "Mail Sorter did not answer /api/health within 30 s"
  return 1
}

release_caddy_lock() {
  if [ -n "$HOLDING_CADDY_LOCK" ]; then
    rm -f "$CADDY_LOCK"
    HOLDING_CADDY_LOCK=""
  fi
}

other_ct_deploys() {
  pgrep -af -- '-deploy-ct\.sh' | grep -v -- 'mail-sorter-deploy-ct' || true
}

seconds_since_caddyfile_changed() {
  echo $(( $(date +%s) - $(stat -c %Y "$CADDYFILE") ))
}

take_caddy_lock() {
  local waited=0 quiet
  trap release_caddy_lock EXIT
  while :; do
    quiet="$(seconds_since_caddyfile_changed)"
    if [ "$quiet" -ge "$CADDY_QUIET_SECONDS" ] && [ -z "$(other_ct_deploys)" ] && ( set -C; echo "mail-sorter deploy, pid $$, $(date -Is)" > "$CADDY_LOCK" ) 2> /dev/null; then
      HOLDING_CADDY_LOCK=yes
      return 0
    fi
    if [ "$waited" -ge "$CADDY_LOCK_WAIT_SECONDS" ]; then
      echo "caddy: gave up after ${waited} s: $CADDY_LOCK is held, another deploy runs or the Caddyfile changed ${quiet} s ago"
      return 1
    fi
    if [ $(( waited % 60 )) = 0 ]; then
      echo "caddy: waiting (lock: $(cat "$CADDY_LOCK" 2> /dev/null || echo free), Caddyfile changed ${quiet} s ago)"
    fi
    sleep 5
    waited=$(( waited + 5 ))
  done
}

copy_database() {
  local target="$1" scratch
  scratch="$(mktemp -d)"
  chown "$SERVICE_USER:$SERVICE_USER" "$scratch"
  runuser -u "$SERVICE_USER" -- python3 -I - "$DATA/mail-sorter.db" "$scratch/copy.db" <<'PY'
import sqlite3, sys
source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
copy = sqlite3.connect(sys.argv[2])
source.backup(copy)
copy.close()
source.close()
PY
  mv "$scratch/copy.db" "$target"
  rmdir "$scratch"
}

do_backup() {
  local stamp backup work
  stamp="$(date +%Y%m%d-%H%M%S)"
  backup="$STATE/mail-sorter-before-$stamp.tgz"
  work="$(mktemp -d)"
  if [ -f "$DATA/mail-sorter.db" ]; then
    copy_database "$work/mail-sorter.db"
  fi
  local parts=(etc/caddy/Caddyfile)
  for path in opt/mail-sorter etc/mail-sorter etc/systemd/system/mail-sorter.service; do
    if [ -e "/$path" ]; then parts+=("$path"); fi
  done
  ( umask 077 && tar -czf "$backup" -C / --exclude=opt/mail-sorter/.venv "${parts[@]}" -C "$work" . )
  rm -rf "$work"
  chmod 600 "$backup"
  echo "$backup" > "$BACKUP_POINTER"
  chmod 600 "$BACKUP_POINTER"
  echo "backup: $backup ($(du -h "$backup" | cut -f1))"
  keep_newest_copies "$STATE/mail-sorter-before-*.tgz" "$KEPT_BACKUPS"
}

port_is_taken_by_another_program() {
  local listeners main_pid
  listeners="$(ss -ltnp "sport = :$PORT" | grep LISTEN || true)"
  [ -n "$listeners" ] || return 1
  main_pid="$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null || true)"
  case "$main_pid" in ''|0) return 0 ;; esac
  ! grep -q "pid=$main_pid," <<< "$listeners"
}

ensure_user_and_folders() {
  if ! id -u "$SERVICE_USER" > /dev/null 2>&1; then
    useradd --system --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "user: made $SERVICE_USER"
  fi
  install -d -m 700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA"
  install -d -m 750 -o root -g "$SERVICE_USER" "$CONF"
}

fill_in_site_values() {
  sed -e "s/__SITE_HOST__/$SITE_HOST/g" "$@"
}

install_env_file() {
  if [ -f "$ENV_FILE" ]; then
    echo "env: $ENV_FILE kept as it is"
    return
  fi
  install -m 640 -o root -g "$SERVICE_USER" /dev/null "$ENV_FILE"
  fill_in_site_values "$APP/deploy/mail-sorter.env" > "$ENV_FILE"
  echo "env: wrote $ENV_FILE (Google client still empty: run ./deploy.sh google from the laptop)"
}

install_token_key() {
  local fresh
  if [ -f "$TOKEN_KEY" ]; then
    echo "token key: $TOKEN_KEY kept as it is"
    return
  fi
  fresh="$(mktemp)"
  python3 -I -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())' > "$fresh"
  install -m 640 -o root -g "$SERVICE_USER" "$fresh" "$TOKEN_KEY"
  rm -f "$fresh"
  echo "token key: made $TOKEN_KEY (root:$SERVICE_USER 640)"
}

install_code() {
  local bundle="$1" stamp="$2" fresh="$APP-new-$2"
  mkdir "$fresh"
  if ! { tar -xzf "$bundle" -C "$fresh" --no-same-owner && python3 -m venv "$fresh/.venv" &&
         "$fresh/.venv/bin/pip" install -q --disable-pip-version-check -r "$fresh/requirements.txt"; }; then
    rm -rf "$fresh" "$bundle"
    echo "refusing: the new code could not be unpacked or its packages installed; nothing was changed"
    exit 1
  fi
  rm -f "$bundle"
  chown -R root:root "$fresh"
  chmod -R u=rwX,go=rX "$fresh"
  if [ -d "$APP" ]; then
    mv "$APP" "$APP-before-$stamp"
    echo "$APP-before-$stamp" > "$BEFORE_POINTER"
  else
    echo none > "$BEFORE_POINTER"
  fi
  mv "$fresh" "$APP"
}

keep_newest_copies() {
  local pattern="$1" kept="${2:-$KEPT_CODE_COPIES}" copies surplus index
  shopt -s nullglob
  copies=($pattern)
  shopt -u nullglob
  surplus=$(( ${#copies[@]} - kept ))
  for (( index = 0; index < surplus; index++ )); do
    rm -rf -- "${copies[index]}"
    echo "old copy removed: ${copies[index]}"
  done
}

prune_old_copies() {
  keep_newest_copies "$APP-before-*"
  keep_newest_copies "$APP-undone-*"
  keep_newest_copies "$CADDYFILE.bak-mail-sorter-[0-9]*"
  keep_newest_copies "$CADDYFILE.bak-mail-sorter-undo-*"
}

marker_count() {
  grep -cxF -- "$1" "$CADDYFILE" || true
}

own_block_state() {
  local begins ends
  begins="$(marker_count "$CADDY_BLOCK_BEGIN")"
  ends="$(marker_count "$CADDY_BLOCK_END")"
  if [ "$begins" = 1 ] && [ "$ends" = 1 ]; then
    echo one
  elif [ "$begins" = 0 ] && [ "$ends" = 0 ]; then
    echo none
  else
    echo damaged
  fi
}

print_own_block() {
  awk -v begin="$CADDY_BLOCK_BEGIN" -v end="$CADDY_BLOCK_END" '
    $0 == begin { inside = 1 }
    inside { print }
    $0 == end { inside = 0 }
  ' "$CADDYFILE"
}

render_caddy_block() {
  printf '%s\n' "$CADDY_BLOCK_BEGIN"
  sed -e '/./,$!d' -e "s/__PORT__/$PORT/g" "$CADDY_BLOCK_SOURCE" | fill_in_site_values
  printf '%s\n' "$CADDY_BLOCK_END"
}

caddyfile_without_own_block() {
  awk -v begin="$CADDY_BLOCK_BEGIN" -v end="$CADDY_BLOCK_END" '
    $0 == begin { inside = 1; held = 0; next }
    inside { if ($0 == end) inside = 0; next }
    held { print ""; held = 0 }
    $0 == "" { held = 1; next }
    { print }
    END { if (held) print "" }
  ' "$CADDYFILE"
}

caddyfile_with_own_block_from() {
  awk -v begin="$CADDY_BLOCK_BEGIN" -v end="$CADDY_BLOCK_END" -v replacement="$1" '
    $0 == begin { inside = 1; while ((getline line < replacement) > 0) print line; next }
    inside { if ($0 == end) inside = 0; next }
    { print }
  ' "$CADDYFILE"
}

caddyfile_is_valid() {
  local report
  if ! command -v caddy > /dev/null 2>&1; then
    echo "caddy: no caddy command here, the Caddyfile was not validated"
    return 0
  fi
  if ! report="$(caddy validate --config "$CADDYFILE" --adapter caddyfile 2>&1)"; then
    printf '%s\n' "$report" | tail -n 5
    return 1
  fi
}

switch_caddyfile() {
  local next="$1" backup="$2"
  cp -a "$CADDYFILE" "$backup" || return 1
  cat "$next" > "$CADDYFILE"
  if ! caddyfile_is_valid; then
    cat "$backup" > "$CADDYFILE"
    echo "caddy: the changed Caddyfile did not validate, so it was put back from $backup"
    return 1
  fi
  if ! systemctl reload caddy; then
    cat "$backup" > "$CADDYFILE"
    echo "caddy: the reload failed, so the Caddyfile was put back from $backup; Caddy keeps the config it was running"
    return 1
  fi
}

add_caddy_site() {
  local stamp="$1" work change
  rm -f "$CADDY_CHANGE" "$CADDY_BLOCK_BEFORE"
  work="$(mktemp -d)"
  case "$(own_block_state)" in
    one)
      render_caddy_block > "$work/block"
      if [ "$(print_own_block)" = "$(cat "$work/block")" ]; then
        rm -rf "$work"
        echo "caddy: the marked $SITE_HOST block is up to date, left as is"
        return 0
      fi
      print_own_block > "$work/before"
      change=replaced
      ;;
    none)
      if grep -qxF "http://$SITE_HOST {" "$CADDYFILE"; then
        rm -rf "$work"
        echo "caddy: an unmarked $SITE_HOST block is already present, left as is"
        return 0
      fi
      change=added
      ;;
    *)
      rm -rf "$work"
      echo "caddy: the mail-sorter markers in $CADDYFILE are damaged (not one begin and one end line); fix them by hand"
      return 1
      ;;
  esac
  if ! take_caddy_lock; then
    rm -rf "$work"
    return 1
  fi
  if [ "$change" = replaced ]; then
    caddyfile_with_own_block_from "$work/block" > "$work/next"
  else
    { cat "$CADDYFILE"; printf '\n'; render_caddy_block; } > "$work/next"
  fi
  if ! switch_caddyfile "$work/next" "$CADDYFILE.bak-mail-sorter-$stamp"; then
    release_caddy_lock
    rm -rf "$work"
    return 1
  fi
  release_caddy_lock
  if [ "$change" = replaced ]; then
    mv "$work/before" "$CADDY_BLOCK_BEFORE"
  fi
  echo "$change" > "$CADDY_CHANGE"
  rm -rf "$work"
  echo "caddy: $change the marked $SITE_HOST block for port $PORT and reloaded (earlier file: $CADDYFILE.bak-mail-sorter-$stamp)"
}

undo_caddy_site() {
  local change state stamp work
  change="$(cat "$CADDY_CHANGE" 2>/dev/null || true)"
  [ -n "$change" ] || return 0
  state="$(own_block_state)"
  if [ "$state" = none ]; then
    rm -f "$CADDY_CHANGE" "$CADDY_BLOCK_BEFORE"
    echo "caddy: the marked mail-sorter block is already gone, Caddyfile left as is"
    return 0
  fi
  if [ "$state" != one ]; then
    echo "caddy: the mail-sorter markers in $CADDYFILE are damaged; nothing changed, fix them by hand"
    return 1
  fi
  case "$change" in
    added|replaced) ;;
    *)
      echo "caddy: $CADDY_CHANGE holds '$change', which this script does not know; nothing changed"
      return 1
      ;;
  esac
  take_caddy_lock || return 1
  stamp="$(date +%Y%m%d-%H%M%S)"
  work="$(mktemp -d)"
  if [ "$change" = added ]; then
    caddyfile_without_own_block > "$work/next"
  else
    caddyfile_with_own_block_from "$CADDY_BLOCK_BEFORE" > "$work/next"
  fi
  if ! switch_caddyfile "$work/next" "$CADDYFILE.bak-mail-sorter-undo-$stamp"; then
    release_caddy_lock
    rm -rf "$work"
    return 1
  fi
  release_caddy_lock
  rm -rf "$work"
  rm -f "$CADDY_CHANGE" "$CADDY_BLOCK_BEFORE"
  echo "caddy: undid the $change mail-sorter block only, other sites untouched (earlier file: $CADDYFILE.bak-mail-sorter-undo-$stamp)"
}

own_block_port() {
  print_own_block | sed -n 's/^[[:space:]]*reverse_proxy 127\.0\.0\.1:\([0-9]\{1,5\}\)[[:space:]]*$/\1/p' | head -n 1
}

caddy_answer() {
  curl -s -o /dev/null -w '%{http_code}' -H "Host: $SITE_HOST" "http://127.0.0.1$1"
}

page_title_through_caddy() {
  curl -s -H "Host: $SITE_HOST" http://127.0.0.1/ | sed -n 's:.*<title>\(.*\)</title>.*:\1:p' | head -n 1
}

do_check() {
  local since="${1:-}" failures=0 health direct page title block_port tracebacks
  load_port
  echo "service: $(systemctl is-active "$SERVICE" || true)"
  health="$(curl -s "$LOCAL/api/health" || true)"
  echo "health: $health"
  direct="$(code_of "$LOCAL/api/next")"
  echo "app without a session: /api/next -> $direct (401 expected; 503 while Google is not set up)"
  page="$(caddy_answer /)"
  title="$(page_title_through_caddy)"
  echo "through Caddy: / -> $page, title: $title, /api/health -> $(caddy_answer /api/health)"
  block_port="$(own_block_port)"
  echo "marked Caddy block sends to port ${block_port:-none}, PORT in $ENV_FILE is $PORT"
  echo "modes: env $(stat -c '%a %U:%G' "$ENV_FILE"), token key $(stat -c '%a %U:%G' "$TOKEN_KEY"), data $(stat -c '%a %U:%G' "$DATA")"
  [ "$(systemctl is-active "$SERVICE" || true)" = active ] || failures=$((failures + 1))
  case "$health" in *'"ok":true'*) ;; *) failures=$((failures + 1)) ;; esac
  case "$direct" in 401|503) ;; *) failures=$((failures + 1)) ;; esac
  [ "$page" = 200 ] || failures=$((failures + 1))
  case "$title" in "Mail Sorter: sign in"|"Mail Sorter: not set up yet") ;; *) failures=$((failures + 1)) ;; esac
  [ "$block_port" = "$PORT" ] || failures=$((failures + 1))
  [ "$(stat -c '%a' "$ENV_FILE")" = 640 ] || failures=$((failures + 1))
  [ "$(stat -c '%a' "$TOKEN_KEY")" = 640 ] || failures=$((failures + 1))
  [ "$(stat -c '%a' "$DATA")" = 700 ] || failures=$((failures + 1))
  if [ -n "$since" ]; then
    tracebacks="$(journalctl -u "$SERVICE" --since "@$since" --no-pager | grep -c Traceback || true)"
    echo "tracebacks in the journal since restart: $tracebacks"
    [ "$tracebacks" = 0 ] || failures=$((failures + 1))
  fi
  echo "check failures: $failures"
  [ "$failures" = 0 ]
}

load_port_for_apply() {
  local code_bundle="$1" bundled_env
  if [ -f "$ENV_FILE" ]; then
    PORT_SOURCE="$ENV_FILE"
    load_port "$ENV_FILE"
    return
  fi
  PORT_SOURCE="deploy/mail-sorter.env"
  bundled_env="$(mktemp)"
  tar -xzOf "$code_bundle" ./deploy/mail-sorter.env > "$bundled_env"
  load_port "$bundled_env"
  rm -f "$bundled_env"
}

do_apply() {
  local code_bundle="$1" stamp since
  [ -s "$BACKUP_POINTER" ] && [ -s "$(cat "$BACKUP_POINTER")" ] || { echo "refusing: run backup first"; exit 1; }
  rm -f "$BEFORE_POINTER" "$CADDY_CHANGE" "$CADDY_BLOCK_BEFORE"
  load_port_for_apply "$code_bundle"
  if port_is_taken_by_another_program; then
    echo "refusing: something other than mail-sorter listens on port $PORT; change PORT in $PORT_SOURCE, the Caddy block follows it"
    exit 1
  fi
  stamp="$(date +%Y%m%d-%H%M%S)"
  ensure_user_and_folders
  install_code "$code_bundle" "$stamp"
  install_env_file
  install_token_key
  install -m 644 "$APP/deploy/mail-sorter.service" "$UNIT"
  systemctl daemon-reload
  systemctl enable "$SERVICE" > /dev/null
  since="$(date +%s)"
  systemctl restart "$SERVICE"
  wait_for_health
  add_caddy_site "$stamp"
  prune_old_copies
  sleep 1
  echo "earlier code: $(cat "$BEFORE_POINTER")"
  do_check "$since"
}

warn_when_block_and_port_differ() {
  local block_port
  echo "env: undo does not change $ENV_FILE (PORT=$PORT)"
  [ "$(own_block_state)" = one ] || return 0
  block_port="$(own_block_port)"
  if [ "$block_port" != "$PORT" ]; then
    echo "WARNING: the Caddy block now sends to port $block_port but mail-sorter listens on $PORT; set PORT=$block_port in $ENV_FILE and restart mail-sorter, or run apply again"
  fi
}

do_undo() {
  local before stamp
  before="$(cat "$BEFORE_POINTER" 2>/dev/null || true)"
  if [ -z "$before" ]; then
    echo "nothing to undo: the last apply changed nothing, or undo already ran"
    return 0
  fi
  if [ "$before" != none ] && [ ! -d "$before" ]; then
    echo "refusing: the earlier code $before is missing, so nothing was changed"
    exit 1
  fi
  load_port
  stamp="$(date +%Y%m%d-%H%M%S)"
  if ! undo_caddy_site; then
    echo "undo stopped before touching the code; the Caddyfile is as it was before undo"
    exit 1
  fi
  if [ "$before" = none ]; then
    systemctl disable --now "$SERVICE" > /dev/null 2>&1 || true
    rm -f "$UNIT"
    systemctl daemon-reload
    mv "$APP" "$APP-undone-$stamp"
    rm -f "$BEFORE_POINTER"
    echo "service stopped and removed, code kept in $APP-undone-$stamp, data left in $DATA, env and token key left in $CONF"
  else
    mv "$APP" "$APP-undone-$stamp"
    mv "$before" "$APP"
    rm -f "$BEFORE_POINTER"
    install -m 644 "$APP/deploy/mail-sorter.service" "$UNIT"
    systemctl daemon-reload
    systemctl restart "$SERVICE"
    wait_for_health
    echo "earlier code back in place, undone code kept in $APP-undone-$stamp"
  fi
  warn_when_block_and_port_differ
  prune_old_copies
}

env_with_google_client() {
  CLIENT_ID="$1" CLIENT_SECRET="$2" awk '
    /^GOOGLE_CLIENT_ID=/ { if (!id_done) print "GOOGLE_CLIENT_ID=" ENVIRON["CLIENT_ID"]; id_done = 1; next }
    /^GOOGLE_CLIENT_SECRET=/ { if (!secret_done) print "GOOGLE_CLIENT_SECRET=" ENVIRON["CLIENT_SECRET"]; secret_done = 1; next }
    { print }
    END {
      if (!id_done) print "GOOGLE_CLIENT_ID=" ENVIRON["CLIENT_ID"]
      if (!secret_done) print "GOOGLE_CLIENT_SECRET=" ENVIRON["CLIENT_SECRET"]
    }
  ' "$ENV_FILE"
}

do_google() {
  local client_id="" client_secret="" next
  IFS= read -r client_id || true
  IFS= read -r client_secret || true
  client_id="$(printf '%s' "$client_id" | tr -d '[:space:]')"
  client_secret="$(printf '%s' "$client_secret" | tr -d '[:space:]')"
  if ! grep -qE "$CLIENT_ID_PATTERN" <<< "$client_id"; then
    echo "refusing: that client id does not end in .apps.googleusercontent.com; nothing was changed"
    exit 1
  fi
  if ! grep -qE "$CLIENT_SECRET_PATTERN" <<< "$client_secret"; then
    echo "refusing: that client secret has characters a Google secret does not have, or is too short; nothing was changed"
    exit 1
  fi
  [ -f "$ENV_FILE" ] || { echo "refusing: $ENV_FILE is missing, run apply first"; exit 1; }
  load_port
  cp -a "$ENV_FILE" "$ENV_FILE.before-google-$(date +%Y%m%d-%H%M%S)"
  keep_newest_copies "$ENV_FILE.before-google-*" "$KEPT_BACKUPS"
  next="$(mktemp "$CONF/.env-XXXXXX")"
  env_with_google_client "$client_id" "$client_secret" > "$next"
  chown "root:$SERVICE_USER" "$next"
  chmod 640 "$next"
  mv "$next" "$ENV_FILE"
  echo "env: GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET written to $ENV_FILE (secret not shown; earlier file kept beside it)"
  systemctl restart "$SERVICE"
  wait_for_health
  echo "health: $(curl -s "$LOCAL/api/health")"
}

case "${1:-}" in
  backup) do_backup ;;
  apply) do_apply "${2:?apply needs the code bundle}" ;;
  check) do_check ;;
  undo) do_undo ;;
  google-set) do_google ;;
  sums) cd "$APP" && find . -type f ! -path './.venv/*' ! -path '*/__pycache__/*' -print0 | sort -z | xargs -0 sha256sum ;;
  *) echo "unknown ct action"; exit 2 ;;
esac
CT_SIDE
}

push_to_ct() {
  local source="$1" target="$2" mode="$3" name
  name="$(basename "$target")"
  scp -q "$source" "$PROXMOX:/root/$name"
  ssh -o BatchMode=yes "$PROXMOX" "pct push $CT /root/$name $target --perms $mode && rm /root/$name"
}

push_ct_script() {
  local local_copy
  local_copy="$(mktemp)"
  write_ct_script > "$local_copy"
  push_to_ct "$local_copy" "$CT_SCRIPT" 700
  rm -f "$local_copy"
}

in_ct() {
  ssh -o BatchMode=yes "$PROXMOX" "pct exec $CT -- env SITE_HOST=$SITE_HOST bash $CT_SCRIPT $*"
}

other_deploys_running() {
  local own_group
  own_group="$(ps -o pgid= -p $$ | tr -d ' ')"
  ps -eo pgid=,pid=,args= | awk -v own="$own_group" '$1 != own && ($3 ~ /deploy\.sh$/ || $4 ~ /deploy\.sh$/) { print }'
}

refuse_while_other_deploys_run() {
  local others
  others="$(other_deploys_running)"
  if [ -n "$others" ]; then
    echo "refusing: another deploy.sh is running on this laptop; try again when it is done:"
    echo "$others"
    exit 1
  fi
}

deploy_code() {
  local commit="$1" work
  work="$(mktemp -d)"
  mkdir "$work/tree"
  git -C "$REPO" archive --format=tar "$commit" | tar -x -C "$work/tree"
  ( cd "$work/tree" && find . -type f -print0 | sort -z | xargs -0 sha256sum ) > "$work/expected.sums"
  tar -czf "$work/code.tgz" -C "$work/tree" .
  push_to_ct "$work/code.tgz" /root/mail-sorter-code.tgz 600
  if ! in_ct apply /root/mail-sorter-code.tgz; then
    rm -rf "$work"
    echo "apply reported failures: read them, then run undo if needed"
    exit 1
  fi
  if diff <(sort -k2 "$work/expected.sums") <(in_ct sums | sort -k2) > /dev/null; then
    echo "sha256: all $(wc -l < "$work/expected.sums") files in /opt/mail-sorter match $(git -C "$REPO" rev-parse --short "$commit")"
  else
    echo "sha256 MISMATCH between /opt/mail-sorter and $commit"
    rm -rf "$work"
    exit 1
  fi
  rm -rf "$work"
}

ask_for_google_client() {
  local client_id client_secret
  read -r -p "Google OAuth client ID: " client_id
  read -r -s -p "Google OAuth client secret (not shown): " client_secret
  echo
  printf '%s\n%s\n' "$client_id" "$client_secret" | ssh -o BatchMode=yes "$PROXMOX" "pct exec $CT -- env SITE_HOST=$SITE_HOST bash $CT_SCRIPT google-set"
}

main() {
  local action="${1:-}"
  case "$action" in
    backup|check|undo|apply|google)
      load_site
      ;;
  esac
  case "$action" in
    backup|check)
      push_ct_script
      in_ct "$action"
      ;;
    undo)
      refuse_while_other_deploys_run
      push_ct_script
      in_ct undo
      ;;
    apply)
      refuse_while_other_deploys_run
      push_ct_script
      deploy_code "${2:-HEAD}"
      ;;
    google)
      push_ct_script
      ask_for_google_client
      ;;
    -h|--help|help)
      usage
      ;;
    *)
      usage
      exit 2
      ;;
  esac
}

main "$@"
