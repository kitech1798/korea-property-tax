"""프로젝트 루트를 import 경로에 올린다.

한글 경로 + Windows 조합에서 pytest의 rootdir 추론이 흔들리는 일이 있어
명시적으로 못 박는다.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
