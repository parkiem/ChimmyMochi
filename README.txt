Jimin-only Multi-thread Voter — GitHub Actions build
======================================================

Get the EXE (no local Python needed):
1) Create a new GitHub repo.
2) Upload:
   - vma_vote_exe.py
   - requirements.txt
   - .github/workflows/build-exe.yml
3) Repo → Actions → run “Build EXE (PyInstaller, Jimin-only)”.
4) Download artifact “vma_vote_exe” → contains vma_vote_exe.exe.

How to run:
- 1 browser, infinite loops:
    vma_vote_exe.exe
- 5 browsers, 10 loops each:
    vma_vote_exe.exe --threads 5 --loops 10
- Headless:
    vma_vote_exe.exe --threads 3 --loops 20 --headless

Behavior:
- Targets **Jimin only**.
- Shows per-vote line with global number and email, e.g.:
    [T2] Vote #7 : michael.jones4729@yahoo.com
- Global vote number resets every time you run the EXE.
- Auto-closes all browsers when finished.
