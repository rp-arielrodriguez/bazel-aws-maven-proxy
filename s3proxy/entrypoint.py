#!/usr/bin/env python3
"""Container entrypoint: fix /data ownership then drop to appuser."""
import os
import pwd


def main():
    pw = pwd.getpwnam("appuser")
    uid, gid = pw.pw_uid, pw.pw_gid

    # Fix /data ownership if needed — named volumes may retain root ownership
    # from before the Dockerfile added appuser.  Skip the walk entirely when
    # the root dir is already correct (common case after first fix).
    data_dir = os.environ.get("CACHE_DIR", "/data")
    st = os.stat(data_dir)
    if st.st_uid != uid or st.st_gid != gid:
        for dirpath, dirnames, filenames in os.walk(data_dir):
            try:
                os.chown(dirpath, uid, gid)
            except OSError:
                pass
            for f in filenames:
                try:
                    os.chown(os.path.join(dirpath, f), uid, gid)
                except OSError:
                    pass

    # Drop privileges
    os.setgid(gid)
    os.initgroups(pw.pw_name, gid)
    os.setuid(uid)
    os.environ["HOME"] = pw.pw_dir
    os.environ["USER"] = pw.pw_name

    port = os.environ.get("PROXY_PORT", "8888")
    os.execvp("gunicorn", [
        "gunicorn",
        "--bind", f"0.0.0.0:{port}",
        "--workers", "2",
        "--threads", "4",
        "--timeout", "120",
        "--access-logfile", "-",
        "app:app",
    ])


if __name__ == "__main__":
    main()
