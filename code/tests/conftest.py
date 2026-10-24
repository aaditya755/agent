import sys
from pathlib import Path

# Add code directory to sys.path
code_dir = Path(__file__).resolve().parents[1]
if str(code_dir) not in sys.path:
    sys.path.insert(0, str(code_dir))
