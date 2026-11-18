import base64
import os
import re
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
OWN = "mail-sorter"
HOST = "mail.example.com"
PORT = 8101
OTHER_APP_PORTS = {"starfire": 8090, "spotmp3": 8095, "keep-notes": 8097, "photos": 8098, "read-later": 8099,
                   "tasks": 8100}
PATH_CONSTANTS = re.compile(r"^(APP|DATA|CONF|UNIT|CADDYFILE|STATE)=/", re.MULTILINE)
CT_SIDE = re.compile(r"^  cat <<'CT_SIDE'\n(.*?)^CT_SIDE$", re.MULTILINE | re.DOTALL)
DISPATCH = re.compile(r"^case \"\$\{1:-\}\" in\n.*?^esac\n", re.MULTILINE | re.DOTALL)
STARFIRE = "http://starfire.example {\n\treverse_proxy 127.0.0.1:8090\n}\n"
TASKS_BLOCK = ("\n# >>> tasks block, added and removed by the tasks deploy.sh\nhttp://tasks.example.com {\n"
               "\treverse_proxy 127.0.0.1:8100\n}\n# <<< tasks block\n")
CLIENT_ID = "123456-abcdef.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-Secret_Value-123"
LAPTOP_SITE = {"PROXMOX": "proxmox-host", "CT": "4242", "SITE_HOST": HOST}
LAPTOP_STUB = '#!/bin/sh\necho "$(basename "$0") $*" >> "$STUB_LOG"\n'

STUBS = {
    "systemctl": """#!/bin/sh
echo "systemctl $*" >> "$STUB_LOG"
case "$1" in
  reload) if [ -e "$BOX/reload-fails" ]; then echo "Job for caddy.service failed." >&2; exit 1; fi ;;
  restart|enable) echo active > "$BOX/active" ;;
  disable) echo inactive > "$BOX/active" ;;
  is-active) cat "$BOX/active" 2>/dev/null || echo inactive ;;
  show) echo 0 ;;
esac
exit 0
""",
    "caddy": """#!/bin/sh
echo "caddy $* lock=$(cat "$LOCK_PATH" 2>/dev/null | cut -c1-18)" >> "$STUB_LOG"
if grep -q BROKEN-DIRECTIVE "$3"; then exit 1; fi
exit 0
""",
    "runuser": '#!/bin/sh\necho "runuser $*" >> "$STUB_LOG"\nshift 3\nexec "$@"\n',
    "chown": '#!/bin/sh\necho "chown $*" >> "$STUB_LOG"\n',
    "id": "#!/bin/sh\nexit 0\n",
    "sleep": "#!/bin/sh\nexit 0\n",
    "journalctl": "#!/bin/sh\nexit 0\n",
    "pgrep": '#!/bin/sh\nif [ -e "$BOX/other-deploy" ]; then echo "4242 bash /root/tasks-deploy-ct.sh apply"; fi\nexit 0\n',
    "ss": "#!/bin/sh\nexit 0\n",
    "install": """#!/bin/sh
args=""
while [ $# -gt 0 ]; do case "$1" in -o|-g) shift ;; *) args="$args $1" ;; esac; shift; done
exec /usr/bin/install $args
""",
    "python3": """#!/bin/sh
if [ "$1" = -m ] && [ "$2" = venv ]; then
  mkdir -p "$3/bin"
  printf '#!/bin/sh\\nif [ -e "$BOX/pip-fails" ]; then echo "ERROR: no matching distribution" >&2; exit 1; fi\\nexit 0\\n' > "$3/bin/pip"
  chmod 755 "$3/bin/pip"
  exit 0
fi
exec /usr/bin/python3 "$@"
""",
    "curl": """#!/bin/sh
format=""; url=""; code=000; body=""
while [ $# -gt 0 ]; do case "$1" in -w) format="$2"; shift ;; -H|-o) shift ;; -s) ;; *) url="$1" ;; esac; shift; done
case "$url" in
  */api/health) if [ ! -e "$BOX/health-down" ]; then code=200; body='{"googleReady":false,"ok":true}'; fi ;;
  */api/next) code=503 ;;
  http://127.0.0.1/) code=200; body='<title>Mail Sorter: not set up yet</title>' ;;
esac
if [ -n "$format" ]; then printf '%s' "$code"; else printf '%s' "$body"; fi
[ "$code" != 000 ]
""",
    "date": """#!/bin/sh
if [ "$1" = "+%Y%m%d-%H%M%S" ]; then
  count=$(( $(cat "$BOX/stamps" 2>/dev/null || echo 0) + 1 ))
  echo "$count" > "$BOX/stamps"
  printf '20261113-%06d\\n' "$count"
  exit 0
fi
exec /usr/bin/date "$@"
""",
}


def ct_script() -> str:
    return CT_SIDE.search((REPO / "deploy.sh").read_text()).group(1)


class Box:
    def __init__(self, root: Path, quiet_seconds: int = 0, lock_wait_seconds: int = 0):
        self.root = root
        self.flags = root / "box"
        self.stubs = root / "stubs"
        for folder in (self.flags, self.stubs, root / "root", root / "etc/caddy", root / "opt", root / "var/lib",
                       root / "etc/systemd/system"):
            folder.mkdir(parents=True)
        for name, body in STUBS.items():
            (self.stubs / name).write_text(body)
            (self.stubs / name).chmod(0o755)
        self.caddyfile = root / "etc/caddy/Caddyfile"
        self.caddyfile.write_text(STARFIRE + TASKS_BLOCK)
        self.make_caddyfile_old()
        text = PATH_CONSTANTS.sub(lambda match: f"{match.group(1)}={root}/", ct_script())
        text = re.sub(r"^CADDY_QUIET_SECONDS=\d+$", f"CADDY_QUIET_SECONDS={quiet_seconds}", text, flags=re.MULTILINE)
        text = re.sub(r"^CADDY_LOCK_WAIT_SECONDS=\d+$", f"CADDY_LOCK_WAIT_SECONDS={lock_wait_seconds}", text, flags=re.MULTILINE)
        self.script = root / "ct.sh"
        self.script.write_text(text)
        self.lock = root / "root" / "caddy-edit.lock"
        self.log = root / "stub.log"
        self.log.touch()
        backup = root / "root" / f"{OWN}-before-test.tgz"
        backup.write_text("stand-in for a real backup\n")
        (root / "root" / f"{OWN}-last-backup").write_text(f"{backup}\n")
        self.env_file = root / "etc" / OWN / f"{OWN}.env"
        self.token_key = root / "etc" / OWN / "token.key"
        self.builds = 0

    def make_caddyfile_old(self) -> None:
        old = time.time() - 3600
        os.utime(self.caddyfile, (old, old))

    def flag(self, name: str, value: str | None = "") -> None:
        path = self.flags / name
        if value is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(value)

    def run(self, *arguments: str, stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(self.script), *arguments], capture_output=True, text=True,
                              env=self.environment(), cwd=self.root, timeout=60, input=stdin)

    def functions(self, shell: str) -> subprocess.CompletedProcess:
        library = self.root / "functions.sh"
        library.write_text(DISPATCH.sub("", self.script.read_text()))
        return self.run_shell(f"source {library}\n{shell}")

    def run_shell(self, shell: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", "-c", shell], capture_output=True, text=True, env=self.environment(),
                              cwd=self.root, timeout=60)

    def environment(self) -> dict:
        return {**os.environ, "PATH": f"{self.stubs}:{os.environ['PATH']}", "STUB_LOG": str(self.log),
                "BOX": str(self.flags), "LOCK_PATH": str(self.lock), "SITE_HOST": HOST}

    def apply(self, label: str) -> subprocess.CompletedProcess:
        self.builds += 1
        tree = self.root / f"build-{self.builds}" / "tree"
        tree.mkdir(parents=True)
        subprocess.run(["cp", "-r", str(REPO / "deploy"), str(REPO / "requirements.txt"), str(tree)], check=True)
        (tree / "VERSION-LABEL").write_text(label)
        code = tree.parent / "code.tgz"
        subprocess.run(["tar", "-czf", str(code), "-C", str(tree), "."], check=True)
        return self.run("apply", str(code))

    def set_port(self, port: int) -> None:
        self.env_file.write_text(re.sub(r"^PORT=.*$", f"PORT={port}", self.env_file.read_text(), flags=re.MULTILINE))

    def code(self) -> str:
        label = self.root / "opt" / OWN / "VERSION-LABEL"
        return label.read_text() if label.exists() else "none"

    def sites(self) -> list[str]:
        return re.findall(r"^http://(\S+) \{$", self.caddyfile.read_text(), flags=re.MULTILINE)

    def block_ports(self) -> list[str]:
        own = self.caddyfile.read_text().split(f"# >>> {OWN} block")[-1]
        return re.findall(r"reverse_proxy 127\.0\.0\.1:(\d+)\n", own) if f"# >>> {OWN} block" in self.caddyfile.read_text() else []

    def service(self) -> str:
        return (self.flags / "active").read_text().strip()


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def test_a_first_apply_installs_and_adds_only_its_own_marked_block(box):
    result = box.apply("v1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.sites() == ["starfire.example", "tasks.example.com", HOST]
    assert box.block_ports() == [str(PORT)]
    assert box.caddyfile.read_text().startswith(STARFIRE + TASKS_BLOCK)
    assert box.service() == "active"
    assert "check failures: 0" in result.stdout


def test_the_env_file_and_token_key_are_made_once_and_kept(box):
    assert box.apply("v1").returncode == 0
    assert stat.S_IMODE(box.env_file.stat().st_mode) == 0o640
    assert stat.S_IMODE(box.token_key.stat().st_mode) == 0o640
    key = box.token_key.read_bytes().strip()
    assert len(base64.urlsafe_b64decode(key)) == 32
    box.env_file.write_text(box.env_file.read_text() + "ALLOWED_ACCOUNTS=drew@example.com\n")
    assert box.apply("v2").returncode == 0
    assert box.token_key.read_bytes().strip() == key
    assert "ALLOWED_ACCOUNTS=drew@example.com" in box.env_file.read_text()


def test_caddy_is_validated_and_reloaded_while_the_shared_lock_is_held_then_released(box):
    assert box.apply("v1").returncode == 0
    log = box.log.read_text()
    assert "caddy validate" in log and "lock=mail-sorter deploy" in log
    assert "systemctl reload caddy" in log
    assert not box.lock.exists()


def test_a_lock_held_by_another_deploy_is_waited_for_and_left_alone(box):
    box.lock.write_text("tasks deploy, pid 1\n")
    result = box.apply("v1")
    assert result.returncode != 0
    assert "caddy-edit.lock is held" in result.stdout
    assert box.lock.read_text() == "tasks deploy, pid 1\n"
    assert box.sites() == ["starfire.example", "tasks.example.com"]
    assert "systemctl reload caddy" not in box.log.read_text()


def test_another_ct_deploy_running_holds_the_caddy_change_back(box):
    box.flag("other-deploy")
    result = box.apply("v1")
    assert result.returncode != 0
    assert HOST not in box.sites()


def test_a_caddyfile_changed_in_the_last_five_minutes_is_left_alone(tmp_path):
    waiting = Box(tmp_path, quiet_seconds=300)
    os.utime(waiting.caddyfile, None)
    result = waiting.apply("v1")
    assert result.returncode != 0
    assert "Caddyfile changed" in result.stdout
    assert HOST not in waiting.sites()
    waiting.make_caddyfile_old()
    waiting.flag("stamps", "10")
    assert waiting.apply("v2").returncode == 0
    assert HOST in waiting.sites()


def test_undo_after_a_first_install_removes_only_its_block_and_stops_the_service(box):
    assert box.apply("v1").returncode == 0
    box.caddyfile.write_text(box.caddyfile.read_text() + "\nhttp://later.example {\n\trespond \"added by hand\"\n}\n")
    box.make_caddyfile_old()
    result = box.run("undo")
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.sites() == ["starfire.example", "tasks.example.com", "later.example"]
    assert box.service() == "inactive"
    assert box.env_file.exists() and box.token_key.exists()
    assert not box.lock.exists()


def test_apply_then_undo_gives_back_the_same_caddyfile(box):
    before = box.caddyfile.read_text()
    assert box.apply("v1").returncode == 0
    box.make_caddyfile_old()
    assert box.run("undo").returncode == 0
    assert box.caddyfile.read_text() == before


def test_an_update_and_its_undo_swap_the_code(box):
    assert box.apply("v1").returncode == 0
    assert box.apply("v2").returncode == 0
    assert box.code() == "v2"
    assert box.run("undo").returncode == 0
    assert box.code() == "v1"
    assert box.run("undo").stdout.strip().startswith("nothing to undo")


def test_a_block_that_does_not_validate_is_taken_out_again(box):
    assert box.apply("v1").returncode == 0
    block = box.root / "opt" / OWN / "deploy" / f"Caddyfile.{OWN}"
    block.write_text(block.read_text() + "\nBROKEN-DIRECTIVE\n")
    before = box.caddyfile.read_text()
    result = box.functions("load_port\nadd_caddy_site 20261113-999999")
    assert result.returncode != 0
    assert "did not validate" in result.stdout
    assert box.caddyfile.read_text() == before
    assert not box.lock.exists()


def test_check_fails_when_the_block_and_the_port_disagree(box):
    assert box.apply("v1").returncode == 0
    box.set_port(9123)
    result = box.run("check")
    assert result.returncode != 0
    assert "marked Caddy block sends to port 8101, PORT in" in result.stdout
    assert "check failures: 1" in result.stdout


def test_a_failed_caddy_reload_puts_the_caddyfile_back(box):
    box.flag("reload-fails")
    result = box.apply("v1")
    assert result.returncode != 0
    assert "put back" in result.stdout
    assert box.caddyfile.read_text() == STARFIRE + TASKS_BLOCK
    assert not box.lock.exists()


def test_an_update_that_fails_at_pip_changes_nothing(box):
    assert box.apply("v1").returncode == 0
    box.flag("pip-fails")
    assert box.apply("v2").returncode != 0
    assert box.code() == "v1"
    assert "nothing to undo" in box.run("undo").stdout


def test_a_new_port_rewrites_the_block_and_undo_brings_the_old_one_back(box):
    assert box.apply("v1").returncode == 0
    box.set_port(9124)
    box.make_caddyfile_old()
    assert box.apply("v2").returncode == 0
    assert box.block_ports() == ["9124"]
    box.make_caddyfile_old()
    undo = box.run("undo")
    assert undo.returncode == 0, undo.stdout + undo.stderr
    assert box.block_ports() == [str(PORT)]
    assert "WARNING: the Caddy block now sends to port 8101 but mail-sorter listens on 9124" in undo.stdout


def test_google_setup_writes_the_client_into_the_env_file_without_showing_the_secret(box):
    assert box.apply("v1").returncode == 0
    result = box.run("google-set", stdin=f"{CLIENT_ID}\n{CLIENT_SECRET}\n")
    assert result.returncode == 0, result.stdout + result.stderr
    env = box.env_file.read_text()
    assert f"GOOGLE_CLIENT_ID={CLIENT_ID}\n" in env and f"GOOGLE_CLIENT_SECRET={CLIENT_SECRET}\n" in env
    assert env.count("GOOGLE_CLIENT_ID=") == 1 and "PUBLIC_URL=https://mail.example.com" in env
    assert CLIENT_SECRET not in result.stdout + result.stderr
    assert stat.S_IMODE(box.env_file.stat().st_mode) == 0o640
    assert "systemctl restart mail-sorter" in box.log.read_text()


@pytest.mark.parametrize("pasted", [
    f"not-a-client-id\n{CLIENT_SECRET}\n",
    f"{CLIENT_ID}\nshort\n",
    f"{CLIENT_ID}\nbad'quote$(reboot)\n",
    "",
])
def test_google_setup_refuses_what_is_not_a_google_client(box, pasted):
    assert box.apply("v1").returncode == 0
    before = box.env_file.read_text()
    result = box.run("google-set", stdin=pasted)
    assert result.returncode != 0
    assert "nothing was changed" in result.stdout
    assert box.env_file.read_text() == before


def test_both_halves_of_the_deploy_script_parse():
    assert subprocess.run(["bash", "-n", str(REPO / "deploy.sh")]).returncode == 0
    assert subprocess.run(["bash", "-n"], input=ct_script(), text=True).returncode == 0


def test_the_port_is_free_of_the_other_apps_and_named_once():
    env = (REPO / "deploy" / f"{OWN}.env").read_text()
    assert re.findall(r"^PORT=(\d+)$", env, flags=re.MULTILINE) == [str(PORT)]
    assert PORT not in OTHER_APP_PORTS.values()
    assert "__PORT__" in (REPO / "deploy" / f"Caddyfile.{OWN}").read_text()
    assert str(PORT) not in (REPO / "deploy.sh").read_text()


def test_the_shipped_env_file_has_no_google_client_in_it():
    env = (REPO / "deploy" / f"{OWN}.env").read_text()
    assert re.findall(r"^GOOGLE_CLIENT_(?:ID|SECRET)=(.*)$", env, flags=re.MULTILINE) == ["", ""]


def test_the_service_stops_restarting_after_a_configuration_mistake():
    unit = (REPO / "deploy" / f"{OWN}.service").read_text()
    assert re.findall(r"^RestartPreventExitStatus=(\d+)$", unit, flags=re.MULTILINE) == ["2"]
    assert "ReadWritePaths=/var/lib/mail-sorter" in unit


TAR_STUB = """#!/bin/sh
while [ $# -gt 0 ]; do if [ "$1" = -czf ]; then echo archive > "$2"; fi; shift; done
"""


def test_backup_keeps_only_the_three_newest_backups(box):
    (box.root / "root" / f"{OWN}-before-test.tgz").unlink()
    (box.stubs / "tar").write_text(TAR_STUB)
    (box.stubs / "tar").chmod(0o755)
    made = []
    for _ in range(5):
        result = box.run("backup")
        assert result.returncode == 0, result.stdout + result.stderr
        made.append(result.stdout.split("backup: ")[1].split(" ")[0])
    left = sorted(str(path) for path in (box.root / "root").glob(f"{OWN}-before-*.tgz"))
    assert left == sorted(made[-3:])
    assert (box.root / "root" / f"{OWN}-last-backup").read_text().strip() == made[-1]


def test_google_setup_keeps_only_the_three_newest_env_copies(box):
    assert box.apply("v1").returncode == 0
    for _ in range(5):
        assert box.run("google-set", stdin=f"{CLIENT_ID}\n{CLIENT_SECRET}\n").returncode == 0
    assert len(list(box.env_file.parent.glob(f"{OWN}.env.before-google-*"))) == 3


def test_the_shipped_files_leave_the_site_host_to_deploy_site_env():
    env = (REPO / "deploy" / f"{OWN}.env").read_text()
    assert re.search(r"^PUBLIC_URL=https://__SITE_HOST__$", env, flags=re.MULTILINE)
    assert (REPO / "deploy" / f"Caddyfile.{OWN}").read_text().startswith("\nhttp://__SITE_HOST__ {\n")


def run_laptop_side(tmp_path, site_text):
    stubs = tmp_path / "laptop-stubs"
    stubs.mkdir()
    for name in ("ssh", "scp"):
        (stubs / name).write_text(LAPTOP_STUB)
        (stubs / name).chmod(0o755)
    log = tmp_path / "laptop.log"
    log.touch()
    site = tmp_path / "site.env"
    if site_text is not None:
        site.write_text(site_text)
    environment = {name: value for name, value in os.environ.items() if name not in LAPTOP_SITE}
    environment.update(PATH=f"{stubs}:{os.environ['PATH']}", STUB_LOG=str(log), SITE_ENV=str(site))
    result = subprocess.run(["bash", str(REPO / "deploy.sh"), "check"], capture_output=True, text=True,
                            env=environment, timeout=60)
    return result, log.read_text()


def test_the_laptop_side_refuses_without_the_site_file(tmp_path):
    result, calls = run_laptop_side(tmp_path, None)
    assert result.returncode == 1
    assert "deploy/site.env.example" in result.stdout
    assert calls == ""


def test_the_laptop_side_passes_the_site_host_to_the_container(tmp_path):
    site_text = "".join(f"{name}={value}\n" for name, value in LAPTOP_SITE.items())
    result, calls = run_laptop_side(tmp_path, site_text)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"ssh -o BatchMode=yes proxmox-host pct exec 4242 -- env SITE_HOST={HOST} bash /root/{OWN}-deploy-ct.sh check" in calls


@pytest.mark.parametrize("name, value", [("SITE_HOST", "mail.example.com;reboot"), ("CT", ""), ("PROXMOX", "proxmox host")])
def test_the_laptop_side_refuses_a_site_value_that_is_not_plain(tmp_path, name, value):
    site_text = "".join(f"{key}={value if key == name else default}\n" for key, default in LAPTOP_SITE.items())
    result, calls = run_laptop_side(tmp_path, site_text)
    assert result.returncode == 1
    assert f"{name} in" in result.stdout
    assert calls == ""
