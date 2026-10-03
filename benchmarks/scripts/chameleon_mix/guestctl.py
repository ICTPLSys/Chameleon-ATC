"""Guest control with Figure 9 runtime paths."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chameleon_mix.common import HA, configure_guest, load
guest = configure_guest(load('concurrent_guest', HA / 'scripts/guestctl.py'))
load_access = guest.load_access
qmp = guest.qmp
