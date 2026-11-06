"""모든 테스트 공통: 실제 AI·실제 DB를 절대 쓰지 않도록 mabujang을 import하기 전에 환경을 고정한다.

(테스트 파일 하나만 따로 실행해도 적용됨. 예전에는 test_flow.py의 설정에 의존해서,
 test_v2.py만 실행하면 실제 Claude와 data/mabujang.db를 써 버린 일이 있었다.)
"""
import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="mabu-test-"))
os.environ.update(MABU_AGENT="mock", MABU_ADMIN_PASSWORD="",
                  MABU_DATA_DIR=str(_tmp / "data"), MABU_WORKSPACES=str(_tmp / "ws"))
