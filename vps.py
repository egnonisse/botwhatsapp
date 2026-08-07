"""Helper: exécute commandes / lit / écrit des fichiers directement sur le VPS.

Usage:
  python vps.py run "<commande shell>"
  python vps.py get <chemin_distant> [<chemin_local>]
  python vps.py put <chemin_local> <chemin_distant>
  python vps.py py <fichier_local_python>     # exécute via le venv du VPS
"""
import sys, paramiko, posixpath

HOST = "169.58.117.185"
USER = "root"
KEY = r"D:\ZARIAMALL\ssh\zariamallssh"
APP = "/opt/botwhatsapp"


def _client():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, key_filename=KEY, timeout=25)
    return c


def run(cmd, timeout=300):
    c = _client()
    _, out, err = c.exec_command(cmd, timeout=timeout)
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    c.close()
    return o + (("\n[stderr]\n" + e) if e.strip() else "")


def get(remote, local=None):
    local = local or posixpath.basename(remote)
    c = _client(); s = c.open_sftp()
    s.get(remote, local); s.close(); c.close()
    return f"downloaded {remote} -> {local}"


def put(local, remote):
    c = _client(); s = c.open_sftp()
    s.put(local, remote); s.close(); c.close()
    return f"uploaded {local} -> {remote}"


def run_py(local_script, keep=False):
    """Envoie un script Python et l'exécute avec le venv du VPS."""
    remote = "/tmp/_vps_exec.py"
    c = _client(); s = c.open_sftp()
    s.put(local_script, remote); s.close()
    cmd = f"cd {APP} && venv/bin/python {remote} 2>&1" + ("" if keep else f"; rm -f {remote}")
    _, out, _ = c.exec_command(cmd, timeout=600)
    res = out.read().decode("utf-8", "replace")
    c.close()
    return res


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__); sys.exit(1)
    if a[0] == "run":
        print(run(a[1]))
    elif a[0] == "get":
        print(get(a[1], a[2] if len(a) > 2 else None))
    elif a[0] == "put":
        print(put(a[1], a[2]))
    elif a[0] == "py":
        print(run_py(a[1]))
    else:
        print(__doc__); sys.exit(1)
