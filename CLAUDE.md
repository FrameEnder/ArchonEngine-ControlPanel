# ArchonEngine ControlPanel (Kawari Web)

Bob's web front end for his FrameEnder/Kawari server. The README is the user guide.

* **The `Windows` branch is this ControlPanel on Windows, and gets every change made here.** After a change on `main`,
  merge `main` into `Windows` in the same session, keeping that branch's Windows adjustments: what runs in a container
  is shared; what runs outside one (`run.sh`, `install-service.sh`, `diagnose.sh`, the Quadlet unit, host paths,
  firewall and Linux commands in the pages) has a Windows counterpart there to change instead. Its `CLAUDE.md` says
  how.

* Every change updates the fork's documentation in the same go: `docs/src/fork/kawari-web.md` (how the feature works)
  and `docs/src/fork/changelog.md` in FrameEnder/Kawari, plus the README section for the feature.
* Bump the version in `kawari_web/__init__.py` and the README for each release.
* No game text or game files in this repository: read them from the server's game install at runtime
  (`kawari-questdump` writes `quest-index.json` and `item-index.json`).
* The world server saves a logged-in character over the database, so edits to `world.db` only for logged-out
  characters (mail is the exception).
