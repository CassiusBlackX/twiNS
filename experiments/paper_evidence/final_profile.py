#!/usr/bin/env python3
"""The same A2 program on one frozen revision; preliminary samples stay in their old directory."""
from pathlib import Path
import os
p=Path(__file__).with_name('profile_runs.py');s=p.read_text()
s=s.replace("local/profile'", "local/profile-final'")
s=s.replace("'build-evidence/twins'","'build-final-clean/twins'").replace("'build-evidence/twins128'","'build-final-clean/twins128'")
s=s.replace("'build-profile/twins'","'build-final-profile/twins'").replace("'build-profile/twins128'","'build-final-profile/twins128'")
s=s.replace("'build-profile57/twins'","'build-final-profile57/twins'").replace("'build-runs57/twins'","'build-final-clean57/twins'")
s=s.replace("'build-mmu/twins-rdma'","'build-final-clean/twins-rdma'").replace("'build-mmu/twins-ai'","'build-final-clean/twins-ai'")
s=s.replace("'build-profile/twins-rdma'","'build-final-profile/twins-rdma'").replace("'build-profile/twins-ai'","'build-final-profile/twins-ai'")
s=s.replace("'build-strict/twins-rdma'","'build-final-strict/twins-rdma'").replace("'build-strict/twins-ai'","'build-final-strict/twins-ai'")
s=s.replace("'profile-rle-build.rc','profile57-build.rc','strict-build.rc','mmu-ai-build.rc','mmu-audit-build.rc','corpus-build.rc','oracle-snapshot-build.rc','micro-build.rc','timer-build.rc','mmu-audit-v3-build.rc'", "'final-clean-build.rc','final-clean57-build.rc','final-profile-build.rc','final-strict-build.rc','final-profile57-build.rc'")
exec(compile(s,str(p),'exec'),{'__file__':str(p),'__name__':'__main__'})
