# Shared files

Files the Python and JavaScript packages both use.

| File | Purpose |
|---|---|
| [`log_cap.py`](log_cap.py) | The platform's per-match log-cap wrapper. Every Big Bad Bots image starts through it: its ENTRYPOINT is `python -I -S -u /opt/chipzen/log_cap.py --` and the bot command is `CMD`. Stdlib only. |
| [`log_cap.py.sha256`](log_cap.py.sha256) | The pinned sha256 of `log_cap.py`. |

`log_cap.py` is a byte-identical copy of the platform's own wrapper
(`src/chipzen/bots/log_cap.py` in chipzen-ai/Chipzen), which is its canonical
home: the platform defines what the cap does and its review checks that every
Big Bad Bots image starts through it. Each Big Bad Bots starter carries its own
copy next to its Dockerfile, so a starter folder copied out of this repo still
builds with `docker build .`.

Do not edit `log_cap.py` here. A change is made in the platform repo first,
then copied here; `python scripts/log_cap_hash.py --sync` copies it into the
starters and rewrites the `.sha256`, and `EXPECTED_DIGEST` in that script and
the platform repo's pin are bumped together. CI's "Log-cap wrapper mirror
guard" job (`python scripts/log_cap_hash.py --check`) fails on any copy that
does not match.
